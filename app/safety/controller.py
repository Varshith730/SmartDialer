"""
app/safety/controller.py
------------------------
Safety Controller — the final permission boundary for all outbound calls.

"Prediction proposes. Safety decides. Allocation executes."

This module is the most important architectural component.

Problem this solves:
    The Predictive Pacing Engine calculates HOW MANY calls it thinks would
    be efficient to start.  But the pacing engine uses historical estimates
    that can be stale.  Real-time conditions may have changed:
      - Agents may have gone offline since the estimate was made.
      - The provider may be degrading.
      - The campaign may have hit its hard limit.
    The Safety Controller independently re-checks the current state and
    either approves, reduces, or rejects the pacing engine's request.

Architectural invariant:
    There is NO code path that allows the pacing engine to bypass this check.
    The pacing engine calls evaluate() and gets back an approved_count.
    It uses that number — nothing more.

Decision types:
    APPROVE            The requested count is safe.  Approved as-is.
    REDUCE             The requested count exceeds safe capacity.
                       Approved at the safe capacity level.
    REJECT             Safe capacity is zero.  No new calls.
    FALLBACK_PROGRESSIVE  Provider or circuit is degraded.  Fall back to the
                          conservative 1-agent-to-1-call strategy.

Safety capacity formula:
    available_agents   = agents in AVAILABLE state right now (real-time read)
    active_calls       = calls in INITIATED + RINGING + ANSWERED + CONNECTED state
    hard_limit_budget  = campaign.max_concurrent_calls - active_calls
    safe_capacity      = min(available_agents, hard_limit_budget)

Why the real-time agent count overrides the prediction:
    If 100 agents were available when the pacing engine calculated 50 calls,
    but by the time we get here 40 agents have gone OFFLINE, there are only
    60 AVAILABLE.  The prediction of 50 is still valid — but we cap at 60
    available, so 50 is still approved.  If agents drop to 30, we REDUCE to 30.
    The pacing engine is never told about the drop; it simply gets a smaller
    approved_count and self-corrects on the next cycle.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.models.call import CallState, TERMINAL_STATES
from app.models.campaign import Campaign
from app.providers.interface import TelecomProvider
from app.repository.state_store import StateStore
from app.safety.circuit_breaker import CircuitBreaker, CircuitState

logger = logging.getLogger(__name__)

# Call states that count as "in-flight" (consuming an agent and a slot).
INFLIGHT_STATES = {
    CallState.RESERVED,
    CallState.INITIATED,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.CONNECTED,
}


class DecisionType(str, enum.Enum):
    """The four possible Safety Controller verdicts."""
    APPROVE              = "APPROVE"
    REDUCE               = "REDUCE"
    REJECT               = "REJECT"
    FALLBACK_PROGRESSIVE = "FALLBACK_PROGRESSIVE"


@dataclass
class SafetyDecision:
    """
    The complete result of a Safety Controller evaluation.

    Fields
    ------
    decision_type       What the controller decided.
    approved_count      How many new calls may be started.
                        Always 0 when decision_type is REJECT.
    requested_count     What the pacing engine asked for.
    reason              Human-readable explanation (logged and returned).
    safe_capacity       What the controller independently calculated as safe.
    available_agents    Real-time available agent count at decision time.
    inflight_calls      Calls currently in progress at decision time.
    circuit_state       The circuit breaker's state at decision time.
    timestamp           When the decision was made.
    """

    decision_type: DecisionType
    approved_count: int
    requested_count: int
    reason: str
    safe_capacity: int
    available_agents: int = 0
    inflight_calls: int = 0
    circuit_state: str = CircuitState.CLOSED.value
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            f"SafetyDecision({self.decision_type.value}: "
            f"requested={self.requested_count}, "
            f"approved={self.approved_count}, "
            f"safe_capacity={self.safe_capacity}, "
            f"reason={self.reason!r})"
        )


class SafetyController:
    """
    Evaluates pacing engine requests and returns a binding approved count.

    The controller is stateless between calls — it reads fresh data from
    the store on every evaluation.  This is deliberate: we never want to
    rely on a cached view of agent availability.

    Parameters
    ----------
    store               The shared state store (read-only in this context).
    circuit_breaker     The circuit breaker for the provider in use.
    global_max_calls    An absolute hard ceiling that overrides all other limits.
                        Defaults to None (no additional global limit).

    Usage
    -----
        controller = SafetyController(store, circuit_breaker)
        decision = controller.evaluate(requested=17, campaign=campaign, provider=provider)
        print(decision)
        # → SafetyDecision(REDUCE: requested=17, approved=10, ...)

        # Only then initiate approved_count calls via the allocator.
        allocator.bulk_allocate(build_requests(decision.approved_count), provider)
    """

    def __init__(
        self,
        store: StateStore,
        circuit_breaker: CircuitBreaker,
        global_max_calls: Optional[int] = None,
    ) -> None:
        self._store = store
        self._circuit_breaker = circuit_breaker
        self._global_max_calls = global_max_calls

        # Decision history for diagnostics and the simulation reporter.
        self._decision_log: list[SafetyDecision] = []

    # ------------------------------------------------------------------
    # Main evaluation entry point
    # ------------------------------------------------------------------

    def evaluate(
        self,
        requested: int,
        campaign: Campaign,
        provider: TelecomProvider,
    ) -> SafetyDecision:
        """
        Evaluate whether `requested` new calls can be safely started.

        This method MUST be called before any call is initiated.
        The returned SafetyDecision.approved_count is the binding limit.

        The pacing engine must accept whatever this method returns.
        There is no API to override or bypass this check.

        Steps
        -----
        1. Sanity-check: requested must be > 0.
        2. Check circuit breaker state.
        3. Check provider health.
        4. Read real-time agent and call counts from the store.
        5. Calculate safe capacity.
        6. Apply campaign hard limit.
        7. Apply global hard limit (if set).
        8. Determine decision type and approved count.
        9. Log and return.
        """
        # ------------------------------------------------------------------
        # Step 1: Trivial case
        # ------------------------------------------------------------------
        if requested <= 0:
            return self._decide(
                decision_type=DecisionType.REJECT,
                approved_count=0,
                requested_count=0,
                reason="Requested count is 0 or negative — nothing to do.",
                safe_capacity=0,
            )

        # ------------------------------------------------------------------
        # Step 2: Circuit breaker check
        # ------------------------------------------------------------------
        circuit_state = self._circuit_breaker.state  # property reads fresh state

        if circuit_state == CircuitState.OPEN:
            decision = self._decide(
                decision_type=DecisionType.REJECT,
                approved_count=0,
                requested_count=requested,
                reason=(
                    f"Circuit breaker OPEN for provider {provider.name!r}. "
                    "Rejecting all new calls until cooldown expires."
                ),
                safe_capacity=0,
                circuit_state=circuit_state.value,
            )
            return decision

        if circuit_state == CircuitState.HALF_OPEN:
            # Provider is recovering — allow at most 1 probe call.
            decision = self._decide(
                decision_type=DecisionType.FALLBACK_PROGRESSIVE,
                approved_count=1,
                requested_count=requested,
                reason=(
                    f"Circuit breaker HALF_OPEN for provider {provider.name!r}. "
                    "Allowing 1 probe call (FALLBACK_PROGRESSIVE)."
                ),
                safe_capacity=1,
                circuit_state=circuit_state.value,
            )
            return decision

        # ------------------------------------------------------------------
        # Step 3: Provider health check (independent of circuit breaker)
        # ------------------------------------------------------------------
        if not provider.is_healthy():
            decision = self._decide(
                decision_type=DecisionType.REJECT,
                approved_count=0,
                requested_count=requested,
                reason=(
                    f"Provider {provider.name!r} reports is_healthy()=False. "
                    "Rejecting all new calls."
                ),
                safe_capacity=0,
                circuit_state=circuit_state.value,
            )
            return decision

        # ------------------------------------------------------------------
        # Step 4: Read real-time state
        # ------------------------------------------------------------------
        available_agents = len(self._store.list_available_agents())
        inflight_count = self._count_inflight_calls(campaign.id)

        # ------------------------------------------------------------------
        # Step 5: Calculate safe capacity
        #
        # We cannot start more calls than there are available agents (agents
        # that can pick up if the call is answered).
        # We also cannot exceed the campaign's max_concurrent_calls ceiling.
        # ------------------------------------------------------------------
        hard_limit_budget = campaign.max_concurrent_calls - inflight_count
        hard_limit_budget = max(0, hard_limit_budget)

        safe_capacity = min(available_agents, hard_limit_budget)

        # ------------------------------------------------------------------
        # Step 6: Apply global hard limit (if configured)
        # ------------------------------------------------------------------
        if self._global_max_calls is not None:
            global_budget = self._global_max_calls - inflight_count
            global_budget = max(0, global_budget)
            safe_capacity = min(safe_capacity, global_budget)

        # ------------------------------------------------------------------
        # Step 7: Make the decision
        # ------------------------------------------------------------------
        if safe_capacity <= 0:
            return self._decide(
                decision_type=DecisionType.REJECT,
                approved_count=0,
                requested_count=requested,
                reason=(
                    f"Safe capacity is 0. "
                    f"available_agents={available_agents}, "
                    f"inflight={inflight_count}, "
                    f"campaign_limit={campaign.max_concurrent_calls}."
                ),
                safe_capacity=0,
                available_agents=available_agents,
                inflight_calls=inflight_count,
                circuit_state=circuit_state.value,
            )

        if requested <= safe_capacity:
            return self._decide(
                decision_type=DecisionType.APPROVE,
                approved_count=requested,
                requested_count=requested,
                reason=(
                    f"Request approved. "
                    f"requested={requested}, safe_capacity={safe_capacity}, "
                    f"available_agents={available_agents}, inflight={inflight_count}."
                ),
                safe_capacity=safe_capacity,
                available_agents=available_agents,
                inflight_calls=inflight_count,
                circuit_state=circuit_state.value,
            )

        # requested > safe_capacity → REDUCE
        return self._decide(
            decision_type=DecisionType.REDUCE,
            approved_count=safe_capacity,
            requested_count=requested,
            reason=(
                f"Request reduced from {requested} to {safe_capacity}. "
                f"available_agents={available_agents}, inflight={inflight_count}, "
                f"campaign_limit={campaign.max_concurrent_calls}."
            ),
            safe_capacity=safe_capacity,
            available_agents=available_agents,
            inflight_calls=inflight_count,
            circuit_state=circuit_state.value,
        )

    # ------------------------------------------------------------------
    # Convenience: apply a decision to bulk-select candidates
    # ------------------------------------------------------------------

    def approved_count_only(
        self,
        requested: int,
        campaign: Campaign,
        provider: TelecomProvider,
    ) -> int:
        """
        Shorthand: returns just the approved_count integer.
        Useful when the caller does not need the full decision object.
        """
        return self.evaluate(requested, campaign, provider).approved_count

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def decision_log(self) -> list[SafetyDecision]:
        """Read-only copy of all decisions made (newest last)."""
        return list(self._decision_log)

    def last_decision(self) -> Optional[SafetyDecision]:
        """The most recent decision, or None if no decisions yet."""
        return self._decision_log[-1] if self._decision_log else None

    def decision_summary(self) -> dict:
        """Aggregate counts of each decision type."""
        counts: dict[str, int] = {}
        for d in self._decision_log:
            counts[d.decision_type.value] = counts.get(d.decision_type.value, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _count_inflight_calls(self, campaign_id: Optional[str]) -> int:
        """
        Count calls in INFLIGHT_STATES for the given campaign.
        These are calls that are consuming an agent slot right now.
        """
        calls = self._store.list_calls(campaign_id)
        return sum(1 for c in calls if c.state in INFLIGHT_STATES)

    def _decide(
        self,
        decision_type: DecisionType,
        approved_count: int,
        requested_count: int,
        reason: str,
        safe_capacity: int,
        available_agents: int = 0,
        inflight_calls: int = 0,
        circuit_state: str = CircuitState.CLOSED.value,
    ) -> SafetyDecision:
        """
        Build a SafetyDecision, log it, append to history, and return it.

        All decisions pass through here so the log is always complete.
        """
        decision = SafetyDecision(
            decision_type=decision_type,
            approved_count=approved_count,
            requested_count=requested_count,
            reason=reason,
            safe_capacity=safe_capacity,
            available_agents=available_agents,
            inflight_calls=inflight_calls,
            circuit_state=circuit_state,
        )

        # Log level depends on outcome.
        if decision_type == DecisionType.APPROVE:
            logger.info("[SafetyController] %s", decision)
        elif decision_type in (DecisionType.REDUCE, DecisionType.FALLBACK_PROGRESSIVE):
            logger.warning("[SafetyController] %s", decision)
        else:  # REJECT
            logger.error("[SafetyController] %s", decision)

        self._decision_log.append(decision)
        return decision
