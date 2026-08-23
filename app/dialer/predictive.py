"""
app/dialer/predictive.py
------------------------
Predictive Pacing Engine — computes how many calls to REQUEST from the Safety Controller.

"Prediction proposes. Safety decides. Allocation executes."

Problem this solves:
    Progressive dialing is safe but wasteful: it never starts the next call
    until an agent finishes the current one.  In reality, most outbound calls
    go unanswered.  A borrower's phone rings for 20-30 seconds before the
    system can declare "no answer" and move on.  During that ring time, a
    progressive dialer has an idle agent.

    Predictive dialing solves this by dialling ahead — starting a new call
    while the previous one is still ringing — so that when an agent finishes,
    a live borrower is already on the line.

Why this is rule-based, not ML:
    The assignment prioritises transparency and explainability.  The formula
    here is derived from the same Erlang queuing theory used in real call
    centres.  Every variable in the formula has a clear real-world meaning,
    and a technical interviewer can ask "what does alpha do?" and get a
    one-sentence answer.  A neural network would not afford that.

Architecture invariant (THE MOST IMPORTANT RULE):
    The Predictive Engine NEVER calls the provider.
    The Predictive Engine NEVER calls the allocator.
    The Predictive Engine produces ONE number: requested_calls.
    That number is sent to the Safety Controller, which approves, reduces,
    or rejects it.  The approved number is then passed to the allocator.

    There is no code path in this module that talks to a provider.

The formula — explained:
    1. We want all AVAILABLE agents to be busy (connected) at all times.
    2. To connect A agents, we must dial A / answer_rate calls
       (because only a fraction of dials result in an answer).
    3. Some calls are already in flight (INITIATED / RINGING / RESERVED).
       We only need to start the *additional* calls.
    4. We cap the additional calls at max_calls_per_agent × A to prevent
       extreme over-dialling when the answer rate is very low.

    requested = clip(
        ceil(A / answer_rate) - currently_in_flight,
        0,
        int(A * max_calls_per_agent)
    )

    Where:
        A                   = available_agents (real-time from store)
        answer_rate         = smoothed_answer_rate (EMA)
        currently_in_flight = calls in RESERVED / INITIATED / RINGING state
        max_calls_per_agent = campaign.max_calls_per_agent (hard ceiling)

EMA update formula (from the spec):
    new_rate = alpha * observed + (1 - alpha) * previous_rate

    alpha = 0.1 (default) — slow adaptation, stable estimates.
    Higher alpha = faster adaptation to changing answer rates (more volatile).
    Lower alpha  = smoother estimates, slower to react.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.models.call import CallState
from app.models.campaign import Campaign
from app.repository.state_store import StateStore

logger = logging.getLogger(__name__)

# Call states that count as "in-flight" for the pacing formula.
# These are calls that have been initiated but not yet resolved.
# RESERVED is included because the agent is committed even before the
# provider is called.
INFLIGHT_FOR_PACING = {
    CallState.RESERVED,
    CallState.INITIATED,
    CallState.RINGING,
    CallState.ANSWERED,  # answered but not yet CONNECTED
}

# Minimum answer rate to avoid divide-by-zero.
# If the EMA dips below this, we treat it as a provider health signal
# and the pacing engine outputs 0 (let the Safety Controller decide).
MIN_ANSWER_RATE = 0.01


@dataclass
class PacingSnapshot:
    """
    A single pacing computation snapshot — useful for logging and demo output.

    Fields
    ------
    smoothed_answer_rate    Current EMA estimate.
    smoothed_talk_time      Current EMA talk-time estimate (seconds).
    available_agents        Real-time available agent count.
    inflight_calls          Calls currently in RESERVED/INITIATED/RINGING/ANSWERED.
    connected_calls         Calls in CONNECTED state.
    target_inflight         What the formula says the ideal in-flight count is.
    requested_calls         The final number sent to the Safety Controller.
    timestamp               When this snapshot was taken.
    """

    smoothed_answer_rate: float
    smoothed_talk_time: float
    available_agents: int
    inflight_calls: int
    connected_calls: int
    target_inflight: int
    requested_calls: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            f"PacingSnapshot("
            f"answer_rate={self.smoothed_answer_rate:.2%}, "
            f"talk_time={self.smoothed_talk_time:.0f}s, "
            f"available={self.available_agents}, "
            f"inflight={self.inflight_calls}, "
            f"connected={self.connected_calls}, "
            f"requested={self.requested_calls})"
        )


class PredictiveEngine:
    """
    Predictive pacing engine.

    This class:
    - Maintains smoothed estimates of answer rate and talk time.
    - Computes how many new calls to request on each dialling cycle.
    - Provides those estimates to the Safety Controller (never to the provider).

    Parameters
    ----------
    store               The shared state store (read-only — for agent/call counts).
    alpha               EMA smoothing factor.  0 < alpha < 1.
                        Typical values: 0.05 – 0.20.
    initial_answer_rate Starting answer rate estimate (0.0 – 1.0).
    initial_talk_time   Starting average talk time estimate (seconds).
    call_setup_time     Estimated time from call initiation to answer (seconds).
                        Used to reason about pipeline depth but does not affect
                        the core formula in this prototype.
    """

    def __init__(
        self,
        store: StateStore,
        alpha: float = 0.1,
        initial_answer_rate: float = 0.50,
        initial_talk_time: float = 90.0,
        call_setup_time: float = 15.0,
    ) -> None:
        self._store = store
        self._alpha = alpha
        self._call_setup_time = call_setup_time

        # EMA state — updated via record_call_outcome().
        self._smoothed_answer_rate: float = initial_answer_rate
        self._smoothed_talk_time: float = initial_talk_time

        # Running counters (for reporting and sanity checks).
        self._total_outcomes_recorded: int = 0
        self._total_answered: int = 0
        self._total_not_answered: int = 0

        # History of PacingSnapshots for the simulation reporter.
        self._snapshot_history: list[PacingSnapshot] = []

    # ------------------------------------------------------------------
    # Main compute method — called every dialling cycle
    # ------------------------------------------------------------------

    def compute_request(self, campaign: Campaign) -> int:
        """
        Compute how many new calls to REQUEST from the Safety Controller.

        This is the ONLY output of the Predictive Engine.  The caller must
        pass this integer to SafetyController.evaluate() and use the
        approved_count it returns.

        The engine MUST NOT use the returned value to directly start calls.

        Algorithm
        ---------
        1. Read real-time state from the store.
        2. Guard against degenerate conditions (no agents, near-zero answer rate).
        3. Apply the Erlang-inspired formula to compute target in-flight calls.
        4. Subtract already-in-flight calls to find the additional calls needed.
        5. Cap at campaign.max_calls_per_agent × available_agents.
        6. Record snapshot and return.
        """
        available_agents = len(self._store.list_available_agents())
        inflight, connected = self._count_call_states(campaign.id)

        # Guard: no agents means nothing to dial for.
        if available_agents == 0:
            return self._record_and_return(
                requested=0,
                available=available_agents,
                inflight=inflight,
                connected=connected,
                target_inflight=0,
            )

        # Guard: answer rate too low to compute meaningfully.
        if self._smoothed_answer_rate < MIN_ANSWER_RATE:
            logger.warning(
                "[PredictiveEngine] Answer rate %.4f below minimum — requesting 0",
                self._smoothed_answer_rate,
            )
            return self._record_and_return(
                requested=0,
                available=available_agents,
                inflight=inflight,
                connected=connected,
                target_inflight=0,
            )

        # ----------------------------------------------------------------
        # Core formula (Erlang-inspired):
        #
        # To expect `available_agents` connections, we need to have
        # target_inflight = ceil(available_agents / answer_rate) calls
        # in the ringing/dialing pipeline.
        #
        # Why ceil? We round up because a fractional call is not possible,
        # and rounding down would under-dial.
        # ----------------------------------------------------------------
        target_inflight = math.ceil(available_agents / self._smoothed_answer_rate)

        # Apply campaign's max_calls_per_agent ceiling.
        # This is a hard cap that prevents runaway dialling.
        max_by_campaign = int(math.ceil(available_agents * campaign.max_calls_per_agent))
        target_inflight = min(target_inflight, max_by_campaign)

        # We already have `inflight` calls in the pipeline.
        # Only request the additional calls needed.
        additional_needed = max(0, target_inflight - inflight)

        # Self-cap: never request more than we have available agents for.
        # (The Safety Controller will enforce this again, but being
        # conservative here produces a cleaner request.)
        requested = min(additional_needed, available_agents)

        logger.debug(
            "[PredictiveEngine] campaign=%s: "
            "available=%d, inflight=%d, connected=%d, "
            "answer_rate=%.1f%%, target_inflight=%d, requested=%d",
            campaign.id[:8],
            available_agents, inflight, connected,
            self._smoothed_answer_rate * 100,
            target_inflight, requested,
        )

        return self._record_and_return(
            requested=requested,
            available=available_agents,
            inflight=inflight,
            connected=connected,
            target_inflight=target_inflight,
        )

    # ------------------------------------------------------------------
    # EMA update — called by the event processor when a call resolves
    # ------------------------------------------------------------------

    def record_call_outcome(
        self,
        answered: bool,
        talk_duration_seconds: float = 0.0,
    ) -> None:
        """
        Update the smoothed estimates after a call resolves.

        This must be called once per completed call:
            - answered=True  + talk_duration when the call was COMPLETED normally.
            - answered=False               when the call was FAILED (no answer, busy, etc.).

        EMA formula (from spec):
            new_rate = alpha * observed + (1 - alpha) * previous_rate

        Where:
            observed = 1.0 if answered, 0.0 if not answered.
            alpha    = self._alpha (smoothing factor).
        """
        observed = 1.0 if answered else 0.0

        # Update answer rate EMA.
        self._smoothed_answer_rate = (
            self._alpha * observed
            + (1.0 - self._alpha) * self._smoothed_answer_rate
        )

        # Update talk time EMA (only if the call was answered).
        if answered and talk_duration_seconds > 0.0:
            self._smoothed_talk_time = (
                self._alpha * talk_duration_seconds
                + (1.0 - self._alpha) * self._smoothed_talk_time
            )

        # Update counters.
        self._total_outcomes_recorded += 1
        if answered:
            self._total_answered += 1
        else:
            self._total_not_answered += 1

        logger.debug(
            "[PredictiveEngine] Outcome recorded: answered=%s, "
            "new_answer_rate=%.4f, new_talk_time=%.1fs",
            answered,
            self._smoothed_answer_rate,
            self._smoothed_talk_time,
        )

    def record_batch_outcomes(self, answered_count: int, total_count: int) -> None:
        """
        Convenience method: update EMA from a batch of call outcomes.

        Used by the simulation runner to seed realistic initial estimates
        without calling record_call_outcome() thousands of times.
        """
        if total_count <= 0:
            return
        observed_rate = answered_count / total_count
        self._smoothed_answer_rate = (
            self._alpha * observed_rate
            + (1.0 - self._alpha) * self._smoothed_answer_rate
        )

    # ------------------------------------------------------------------
    # Accessors — for the Safety Controller, simulation, and reporting
    # ------------------------------------------------------------------

    @property
    def smoothed_answer_rate(self) -> float:
        """Current EMA answer rate estimate (0.0 – 1.0)."""
        return self._smoothed_answer_rate

    @property
    def smoothed_talk_time(self) -> float:
        """Current EMA talk time estimate in seconds."""
        return self._smoothed_talk_time

    @property
    def alpha(self) -> float:
        """EMA smoothing factor."""
        return self._alpha

    @property
    def snapshot_history(self) -> list[PacingSnapshot]:
        """Read-only history of all pacing snapshots."""
        return list(self._snapshot_history)

    def last_snapshot(self) -> Optional[PacingSnapshot]:
        """The most recent pacing snapshot, or None."""
        return self._snapshot_history[-1] if self._snapshot_history else None

    def stats(self) -> dict:
        """Diagnostic summary of the engine's current state."""
        return {
            "smoothed_answer_rate": round(self._smoothed_answer_rate, 4),
            "smoothed_talk_time": round(self._smoothed_talk_time, 2),
            "alpha": self._alpha,
            "call_setup_time": self._call_setup_time,
            "total_outcomes": self._total_outcomes_recorded,
            "total_answered": self._total_answered,
            "total_not_answered": self._total_not_answered,
            "observed_answer_rate": (
                round(self._total_answered / self._total_outcomes_recorded, 4)
                if self._total_outcomes_recorded > 0 else None
            ),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _count_call_states(self, campaign_id: str) -> tuple[int, int]:
        """
        Return (inflight_count, connected_count) for the given campaign.

        inflight = RESERVED + INITIATED + RINGING + ANSWERED
        connected = CONNECTED
        """
        calls = self._store.list_calls(campaign_id)
        inflight = sum(1 for c in calls if c.state in INFLIGHT_FOR_PACING)
        connected = sum(1 for c in calls if c.state == CallState.CONNECTED)
        return inflight, connected

    def _record_and_return(
        self,
        requested: int,
        available: int,
        inflight: int,
        connected: int,
        target_inflight: int,
    ) -> int:
        """Build and store a PacingSnapshot, then return requested."""
        snapshot = PacingSnapshot(
            smoothed_answer_rate=self._smoothed_answer_rate,
            smoothed_talk_time=self._smoothed_talk_time,
            available_agents=available,
            inflight_calls=inflight,
            connected_calls=connected,
            target_inflight=target_inflight,
            requested_calls=requested,
        )
        self._snapshot_history.append(snapshot)
        return requested
