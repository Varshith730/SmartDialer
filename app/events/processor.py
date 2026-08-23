"""
app/events/processor.py
------------------------
Event Processor — routes provider events to the correct call and
updates all downstream state consistently.

Problem this solves:
    Telecom providers deliver events asynchronously via callbacks.
    Those events must be:
    1. Matched to the correct call record.
    2. Applied only if they are valid transitions (not out-of-order).
    3. Ignored if the event_id has already been processed (idempotency).
    4. Reflected in agent and borrower state when a call terminates.
    5. Reported to the pacing engine so it can update its EMA estimates.

Thread safety:
    process() can be called from any thread (provider daemon threads call
    the callback which calls process()).  All mutations go through the
    StateStore which uses per-row locks.  The processor itself holds no
    shared mutable state beyond the store reference.

Key invariants maintained here:
    Invariant 4: Duplicate provider events are idempotent.
                 → enforced by call.apply_transition(event_id=...)
    Invariant 5: Stale/out-of-order events cannot corrupt terminal state.
                 → enforced by call.apply_transition()'s rank check
    Invariant 6: Worker crashes are recoverable (lease expiry).
                 → supported by the lease fields already set at allocation

Agent lifecycle after a call terminates:
    COMPLETED call → agent moves to WRAP_UP (then AVAILABLE after wrap-up).
    FAILED / CANCELLED call → agent moves directly to AVAILABLE.

Borrower lifecycle after a call terminates:
    COMPLETED (answered, promise/contact made) → BorrowerStatus.COMPLETED.
    FAILED / CANCELLED → BorrowerStatus.PENDING (retry eligible).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.models.agent import AgentState
from app.models.borrower import BorrowerStatus
from app.models.call import Call, CallState, TERMINAL_STATES
from app.providers.interface import ProviderEvent
from app.repository.state_store import StateStore

logger = logging.getLogger(__name__)

# Map provider event_type strings to CallState enum values.
# Only these strings are accepted; anything else is logged and dropped.
EVENT_TYPE_TO_STATE: dict[str, CallState] = {
    "RINGING":   CallState.RINGING,
    "ANSWERED":  CallState.ANSWERED,
    "CONNECTED": CallState.CONNECTED,
    "COMPLETED": CallState.COMPLETED,
    "FAILED":    CallState.FAILED,
    "CANCELLED": CallState.CANCELLED,
}


class EventProcessor:
    """
    Processes ProviderEvents and maintains system-wide consistency.

    Usage
    -----
        processor = EventProcessor(store, pacing_engine=engine)

        # Register with a provider:
        provider.initiate_call(call, callback=processor.process)

        # Or call directly (e.g. from tests):
        result = processor.process(ProviderEvent(
            event_id="evt-001", call_id=call.id, event_type="RINGING"
        ))

    Parameters
    ----------
    store           Shared state store.
    pacing_engine   Optional PredictiveEngine.  If provided, completed and
                    failed calls are reported so the EMA estimates update.
    wrap_up_seconds How long an agent stays in WRAP_UP before becoming
                    AVAILABLE again.  0 = immediate release (useful for tests).
    """

    def __init__(
        self,
        store: StateStore,
        pacing_engine=None,        # PredictiveEngine | None (avoid circular import)
        wrap_up_seconds: float = 0.0,
    ) -> None:
        self._store = store
        self._pacing_engine = pacing_engine
        self._wrap_up_seconds = wrap_up_seconds

        # Counters for the simulation reporter.
        self._total_events_received: int = 0
        self._total_events_applied: int = 0
        self._total_duplicates_dropped: int = 0
        self._total_out_of_order_dropped: int = 0
        self._total_unknown_calls: int = 0

    # ------------------------------------------------------------------
    # Main entry point (used as the EventCallback)
    # ------------------------------------------------------------------

    def process(self, event: ProviderEvent) -> bool:
        """
        Process a single provider event.

        Returns True if the event caused a state transition.
        Returns False if the event was dropped (duplicate, stale, unknown call).

        This method is safe to call from multiple threads simultaneously.
        """
        self._total_events_received += 1

        # ------------------------------------------------------------------
        # Step 1: Look up the call.
        # ------------------------------------------------------------------
        call = self._store.get_call(event.call_id)
        if call is None:
            logger.warning(
                "[EventProcessor] Unknown call_id %s from event %s",
                event.call_id[:8], event.event_id[:8],
            )
            self._total_unknown_calls += 1
            return False

        # ------------------------------------------------------------------
        # Step 2: Map event_type to a CallState.
        # ------------------------------------------------------------------
        new_state = EVENT_TYPE_TO_STATE.get(event.event_type)
        if new_state is None:
            logger.warning(
                "[EventProcessor] Unknown event_type %r for call %s",
                event.event_type, event.call_id[:8],
            )
            return False

        # ------------------------------------------------------------------
        # Step 3: Apply the transition (idempotency + ordering enforced here).
        #
        # call.apply_transition() internally:
        #   a. Checks if event_id is already in processed_event_ids → drops it.
        #   b. Checks if the new state's rank > current state's rank → drops
        #      backwards/lateral moves.
        #   c. On success: increments version, records event_id, updates timestamps.
        # ------------------------------------------------------------------
        applied = call.apply_transition(new_state, event_id=event.event_id)

        if not applied:
            # Determine whether it was a duplicate or out-of-order for metrics.
            if call.has_processed(event.event_id):
                self._total_duplicates_dropped += 1
                logger.debug(
                    "[EventProcessor] Duplicate event %s (type=%s) dropped for call %s",
                    event.event_id[:8], event.event_type, event.call_id[:8],
                )
            else:
                self._total_out_of_order_dropped += 1
                logger.debug(
                    "[EventProcessor] Out-of-order/stale event %s (type=%s → %s) dropped for call %s",
                    event.event_id[:8], event.event_type,
                    call.state.value, event.call_id[:8],
                )
            return False

        # ------------------------------------------------------------------
        # Step 4: Persist the updated call.
        # ------------------------------------------------------------------
        self._store.save_call(call)
        self._total_events_applied += 1

        logger.info(
            "[EventProcessor] %s → %s (call=%s, version=%d)",
            event.event_type, new_state.value, event.call_id[:8], call.version,
        )

        # ------------------------------------------------------------------
        # Step 5: Handle terminal state side-effects.
        # ------------------------------------------------------------------
        if new_state in TERMINAL_STATES:
            self._handle_terminal(call, new_state)

        return True

    # ------------------------------------------------------------------
    # Wrap-up completion (called externally by the simulation scheduler)
    # ------------------------------------------------------------------

    def complete_wrap_up(self, agent_id: str) -> None:
        """
        Move an agent from WRAP_UP → AVAILABLE.

        The simulation runner calls this after wrap_up_seconds elapse.
        In a real system this would be triggered by an agent pressing
        "Ready" in their softphone UI.
        """
        agent = self._store.get_agent(agent_id)
        if agent and agent.state == AgentState.WRAP_UP:
            agent.state = AgentState.AVAILABLE
            agent.call_id = None
            agent.borrower_id = None
            agent.reservation_id = None
            agent.lease_until = None
            self._store.save_agent(agent)
            logger.debug("[EventProcessor] Agent %s WRAP_UP → AVAILABLE", agent_id[:8])

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return processing statistics."""
        return {
            "total_received": self._total_events_received,
            "total_applied": self._total_events_applied,
            "duplicates_dropped": self._total_duplicates_dropped,
            "out_of_order_dropped": self._total_out_of_order_dropped,
            "unknown_calls": self._total_unknown_calls,
        }

    # ------------------------------------------------------------------
    # Internal: terminal state handling
    # ------------------------------------------------------------------

    def _handle_terminal(self, call: Call, terminal_state: CallState) -> None:
        """
        Perform all side-effects when a call reaches a terminal state.

        - Notify the pacing engine (update EMA estimates).
        - Update agent state (WRAP_UP for completed, AVAILABLE for failed).
        - Update borrower status.
        """
        answered = (terminal_state == CallState.COMPLETED)
        talk_duration = self._compute_talk_duration(call)

        # Notify pacing engine.
        if self._pacing_engine is not None:
            self._pacing_engine.record_call_outcome(
                answered=answered,
                talk_duration_seconds=talk_duration,
            )

        # Update agent.
        if call.agent_id:
            self._update_agent_on_terminal(call.agent_id, answered)

        # Update borrower.
        if call.borrower_id:
            self._update_borrower_on_terminal(call.borrower_id, answered)

    def _update_agent_on_terminal(self, agent_id: str, answered: bool) -> None:
        """
        Move agent to appropriate state after call ends.

        Answered (COMPLETED) → WRAP_UP   (agent needs to finish notes).
        Not answered (FAILED/CANCELLED) → AVAILABLE (agent is immediately free).
        """
        agent = self._store.get_agent(agent_id)
        if agent is None:
            return

        if answered:
            agent.state = AgentState.WRAP_UP
        else:
            # No talk time → agent immediately available.
            agent.state = AgentState.AVAILABLE

        agent.call_id = None
        agent.borrower_id = None
        agent.reservation_id = None
        agent.lease_until = None
        self._store.save_agent(agent)

        logger.debug(
            "[EventProcessor] Agent %s → %s (answered=%s)",
            agent_id[:8], agent.state.value, answered,
        )

    def _update_borrower_on_terminal(self, borrower_id: str, answered: bool) -> None:
        """
        Update borrower status when their call ends.

        Answered → BorrowerStatus.COMPLETED (do not retry in this campaign run).
        Not answered → BorrowerStatus.PENDING (eligible for retry).
        """
        borrower = self._store.get_borrower(borrower_id)
        if borrower is None:
            return

        if answered:
            borrower.status = BorrowerStatus.COMPLETED
        else:
            borrower.status = BorrowerStatus.PENDING
            borrower.reserved_by = None

        self._store.save_borrower(borrower)

    def _compute_talk_duration(self, call: Call) -> float:
        """
        Compute the talk duration of a call in seconds.

        Uses connected_at → ended_at.  Returns 0 if timestamps are missing.
        """
        if call.connected_at is None or call.ended_at is None:
            return 0.0
        delta = (call.ended_at - call.connected_at).total_seconds()
        return max(0.0, delta)
