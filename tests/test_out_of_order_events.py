"""
tests/test_out_of_order_events.py
----------------------------------
Out-of-order event tests.

Scenarios tested:
  1. COMPLETED arrives before RINGING — call becomes COMPLETED, RINGING ignored.
  2. ANSWERED arrives after COMPLETED — ignored.
  3. RINGING arrives after CONNECTED — ignored.
  4. Full scrambled sequence from Provider B — call still ends in a terminal state.
  5. Multiple out-of-order + duplicates mixed together.
  6. Terminal state is a black hole: no sequence of events can escape it.
  7. Out-of-order metrics tracked by the processor.

Design note:
    All ordering protection lives in call.apply_transition().
    The EventProcessor simply calls it and checks the return value.
    These tests verify that the two layers (model + processor) work
    together correctly under adversarial event orderings.
"""

import pytest

from app.events.processor import EventProcessor
from app.models.call import Call, CallState, TERMINAL_STATES
from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerStatus
from app.providers.interface import ProviderEvent
from app.providers.provider_b import ProviderB
from app.repository.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(call_id: str, event_type: str, event_id: str = None) -> ProviderEvent:
    return ProviderEvent(
        event_id=event_id or f"evt-{event_type.lower()}",
        call_id=call_id,
        event_type=event_type,
    )


def make_processor_with_call(
    initial_state: CallState = CallState.QUEUED,
) -> tuple[EventProcessor, Call, StateStore]:
    store = StateStore()
    call = Call()
    # Walk call to initial state if needed.
    order = [
        CallState.RESERVED, CallState.INITIATED, CallState.RINGING,
        CallState.ANSWERED, CallState.CONNECTED,
    ]
    for s in order:
        if s == initial_state:
            call.apply_transition(s, event_id=f"init-{s.value}")
            break
        call.apply_transition(s, event_id=f"init-{s.value}")

    store.save_call(call)
    processor = EventProcessor(store)
    return processor, call, store


# ===========================================================================
# Call model out-of-order tests
# ===========================================================================

class TestCallModelOrdering:

    def test_completed_before_ringing_accepted_then_ringing_rejected(self):
        """
        Scenario from the spec: COMPLETED, ANSWERED, RINGING
        First event (COMPLETED) is accepted (any non-terminal can go to terminal).
        Subsequent events are rejected (terminal is a black hole).
        """
        call = Call()  # starts QUEUED
        ok_comp = call.apply_transition(CallState.COMPLETED, event_id="e-comp")
        ok_ans  = call.apply_transition(CallState.ANSWERED,  event_id="e-ans")
        ok_ring = call.apply_transition(CallState.RINGING,   event_id="e-ring")

        assert ok_comp is True
        assert ok_ans  is False
        assert ok_ring is False
        assert call.state == CallState.COMPLETED
        assert call.version == 1

    def test_backwards_ringing_after_connected(self):
        """RINGING arriving after CONNECTED must be silently dropped."""
        call = Call(state=CallState.CONNECTED)
        ok = call.apply_transition(CallState.RINGING, event_id="late-ring")
        assert ok is False
        assert call.state == CallState.CONNECTED

    def test_answered_after_completed_rejected(self):
        call = Call(state=CallState.CONNECTED)
        call.apply_transition(CallState.COMPLETED, event_id="comp")
        ok = call.apply_transition(CallState.ANSWERED, event_id="late-ans")
        assert ok is False
        assert call.state == CallState.COMPLETED

    def test_all_terminal_states_block_everything(self):
        """Every terminal state must reject all other states."""
        for terminal in TERMINAL_STATES:
            for target in CallState:
                call = Call(state=terminal)
                result = call.apply_transition(target, event_id=f"evt-{target.value}")
                assert result is False, (
                    f"Expected {terminal} → {target} to be rejected, got True"
                )

    def test_same_state_not_accepted(self):
        """A state cannot transition to itself (state rank is not strictly greater)."""
        call = Call(state=CallState.RINGING)
        ok = call.apply_transition(CallState.RINGING, event_id="same-ring")
        assert ok is False

    def test_full_out_of_order_scramble(self):
        """
        Deliver all events in a scrambled order.
        The call should end up in the FIRST terminal state that arrived.
        """
        call = Call()
        # Scrambled sequence: COMPLETED → ANSWERED → RINGING → RESERVED → INITIATED
        events = [
            (CallState.COMPLETED, "e1"),
            (CallState.ANSWERED,  "e2"),
            (CallState.RINGING,   "e3"),
            (CallState.RESERVED,  "e4"),
            (CallState.INITIATED, "e5"),
        ]
        results = [call.apply_transition(s, eid) for s, eid in events]

        # Only the first (COMPLETED) should succeed.
        assert results[0] is True
        assert all(r is False for r in results[1:])
        assert call.state == CallState.COMPLETED
        assert call.version == 1


# ===========================================================================
# EventProcessor out-of-order tests
# ===========================================================================

class TestEventProcessorOrdering:

    def test_processor_drops_backwards_event(self):
        processor, call, store = make_processor_with_call(CallState.CONNECTED)

        # Try to send RINGING (backwards).
        ok = processor.process(make_event(call.id, "RINGING", "late-ring"))
        assert ok is False
        assert store.get_call(call.id).state == CallState.CONNECTED

    def test_processor_accepts_terminal_from_any_non_terminal(self):
        """From any non-terminal state, FAILED and COMPLETED are valid."""
        for start_state in [
            CallState.QUEUED, CallState.INITIATED,
            CallState.RINGING, CallState.CONNECTED,
        ]:
            store = StateStore()
            call = Call(state=start_state)
            store.save_call(call)
            processor = EventProcessor(store)

            ok = processor.process(make_event(call.id, "FAILED", f"fail-{call.id}"))
            assert ok is True, f"Expected FAILED to be accepted from {start_state}"

    def test_processor_completed_then_answered_ignored(self):
        """Once COMPLETED, ANSWERED must not resurrect the call."""
        processor, call, store = make_processor_with_call(CallState.CONNECTED)

        processor.process(make_event(call.id, "COMPLETED", "comp-1"))
        processor.process(make_event(call.id, "ANSWERED",  "ans-late"))

        call_after = store.get_call(call.id)
        assert call_after.state == CallState.COMPLETED

    def test_out_of_order_metrics_tracked(self):
        processor, call, store = make_processor_with_call(CallState.CONNECTED)

        processor.process(make_event(call.id, "RINGING",  "r1"))  # backwards
        processor.process(make_event(call.id, "ANSWERED", "a1"))  # backwards

        stats = processor.stats()
        assert stats["out_of_order_dropped"] == 2

    def test_agent_not_released_twice_on_out_of_order_failed(self):
        """
        FAILED then duplicate FAILED: agent must be released only once.
        Agent should remain AVAILABLE after both events.
        """
        store = StateStore()
        agent = Agent(name="AgentZ")
        borrower = Borrower(name="BorrowerZ")
        store.save_agent(agent)
        store.save_borrower(borrower)

        call = Call(
            agent_id=agent.id,
            borrower_id=borrower.id,
            state=CallState.RINGING,
        )
        store.save_call(call)

        processor = EventProcessor(store)

        # First FAILED: agent released to AVAILABLE.
        processor.process(make_event(call.id, "FAILED", "fail-1"))
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

        # Second FAILED (same event_id, duplicate): must be dropped.
        processor.process(make_event(call.id, "FAILED", "fail-1"))

        # Agent still AVAILABLE, not double-released.
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

    def test_wrap_up_then_completed_does_not_re_wrap(self):
        """
        If the agent is already in WRAP_UP (from first COMPLETED),
        a second COMPLETED (different event_id but same direction) must be
        a no-op because the call is already in terminal state.
        """
        store = StateStore()
        agent = Agent(name="AgentQ")
        store.save_agent(agent)

        call = Call(agent_id=agent.id, state=CallState.CONNECTED)
        store.save_call(call)

        processor = EventProcessor(store)

        # First COMPLETED → WRAP_UP
        processor.process(make_event(call.id, "COMPLETED", "c1"))
        assert store.get_agent(agent.id).state == AgentState.WRAP_UP

        # Manually complete wrap-up → AVAILABLE.
        processor.complete_wrap_up(agent.id)
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

        # Second COMPLETED (different event_id but call is terminal) → dropped.
        processor.process(make_event(call.id, "COMPLETED", "c2"))  # different id
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE  # unchanged


# ===========================================================================
# ProviderB inject_out_of_order end-to-end
# ===========================================================================

class TestProviderBOutOfOrder:

    def _run_sequence(self, events: list[tuple[str, str]]) -> tuple[Call, StateStore]:
        """
        Build a call in QUEUED state, deliver events via ProviderB injection,
        and return the final call and store.
        """
        store = StateStore()
        call = Call()
        store.save_call(call)
        processor = EventProcessor(store)
        provider = ProviderB(delay_scale=0.0)

        provider.inject_out_of_order(
            call_id=call.id,
            events=events,
            callback=processor.process,
        )

        return store.get_call(call.id), store

    def test_completed_answered_ringing_sequence(self):
        """
        The spec example: COMPLETED, ANSWERED, RINGING
        Call must end in COMPLETED (not resurrected by later events).
        """
        call, _ = self._run_sequence([
            ("COMPLETED", "e1"),
            ("ANSWERED",  "e2"),
            ("RINGING",   "e3"),
        ])
        assert call.state == CallState.COMPLETED
        assert call.version == 1

    def test_failed_then_ringing_then_answered(self):
        """FAILED first → call is terminal → RINGING and ANSWERED dropped."""
        call, _ = self._run_sequence([
            ("FAILED",   "f1"),
            ("RINGING",  "r1"),
            ("ANSWERED", "a1"),
        ])
        assert call.state == CallState.FAILED
        assert call.version == 1

    def test_full_forward_sequence_accepted(self):
        """When events arrive in correct order, all should be applied."""
        call, _ = self._run_sequence([
            ("RINGING",   "e1"),
            ("ANSWERED",  "e2"),
            ("CONNECTED", "e3"),
            ("COMPLETED", "e4"),
        ])
        assert call.state == CallState.COMPLETED
        assert call.version == 4

    def test_mixed_duplicates_and_out_of_order(self):
        """
        Mix of duplicates and out-of-order:
        COMPLETED, COMPLETED(dup), ANSWERED, RINGING, RINGING(dup)
        Call must end COMPLETED, version=1 only.
        """
        call, _ = self._run_sequence([
            ("COMPLETED", "c1"),
            ("COMPLETED", "c1"),  # duplicate
            ("ANSWERED",  "a1"),  # out of order (after terminal)
            ("RINGING",   "r1"),  # out of order
            ("RINGING",   "r1"),  # duplicate of above
        ])
        assert call.state == CallState.COMPLETED
        assert call.version == 1

    def test_call_version_reflects_only_applied_transitions(self):
        """Version should count only transitions that were actually applied."""
        call, _ = self._run_sequence([
            ("RINGING",   "e1"),  # applied
            ("RINGING",   "e1"),  # duplicate → dropped
            ("ANSWERED",  "e2"),  # applied
            ("COMPLETED", "e3"),  # applied
            ("RINGING",   "e4"),  # out of order → dropped
        ])
        # Only 3 transitions were applied: RINGING, ANSWERED, COMPLETED.
        assert call.version == 3
        assert call.state == CallState.COMPLETED
