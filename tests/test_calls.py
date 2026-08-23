"""
tests/test_calls.py
--------------------
Unit tests for the Call model.

Tests verify:
- Default construction and state
- Valid state transitions
- Invalid / backwards state transitions are rejected
- Terminal state is a black hole (nothing can move out)
- Idempotency: duplicate event_id is silently ignored
- Out-of-order events: COMPLETED then RINGING does not resurrect the call
- Version increments on every successful transition
"""

import pytest
from app.models.call import Call, CallState, TERMINAL_STATES


class TestCallModel:

    def test_default_state_is_queued(self):
        call = Call()
        assert call.state == CallState.QUEUED

    def test_default_version_is_zero(self):
        call = Call()
        assert call.version == 0

    def test_each_call_has_unique_id(self):
        assert Call().id != Call().id

    # ------------------------------------------------------------------
    # can_transition_to
    # ------------------------------------------------------------------

    def test_can_transition_forward(self):
        call = Call()
        assert call.can_transition_to(CallState.RESERVED) is True
        assert call.can_transition_to(CallState.INITIATED) is True

    def test_cannot_transition_backward(self):
        call = Call(state=CallState.CONNECTED)
        assert call.can_transition_to(CallState.RINGING) is False
        assert call.can_transition_to(CallState.QUEUED) is False

    def test_cannot_transition_to_same_state(self):
        call = Call(state=CallState.RINGING)
        assert call.can_transition_to(CallState.RINGING) is False

    def test_terminal_state_blocks_all_transitions(self):
        for terminal in TERMINAL_STATES:
            call = Call(state=terminal)
            for other in CallState:
                assert call.can_transition_to(other) is False, (
                    f"Expected {terminal} → {other} to be rejected"
                )

    def test_any_non_terminal_can_go_to_failed(self):
        """FAILED is always reachable from any non-terminal state."""
        for state in CallState:
            if state not in TERMINAL_STATES:
                call = Call(state=state)
                assert call.can_transition_to(CallState.FAILED) is True

    def test_any_non_terminal_can_go_to_cancelled(self):
        for state in CallState:
            if state not in TERMINAL_STATES:
                call = Call(state=state)
                assert call.can_transition_to(CallState.CANCELLED) is True

    # ------------------------------------------------------------------
    # apply_transition — success path
    # ------------------------------------------------------------------

    def test_apply_transition_returns_true_on_success(self):
        call = Call()
        ok = call.apply_transition(CallState.RESERVED)
        assert ok is True

    def test_apply_transition_updates_state(self):
        call = Call()
        call.apply_transition(CallState.RESERVED)
        assert call.state == CallState.RESERVED

    def test_apply_transition_increments_version(self):
        call = Call()
        call.apply_transition(CallState.RESERVED)
        assert call.version == 1
        call.apply_transition(CallState.INITIATED)
        assert call.version == 2

    def test_apply_transition_records_initiated_at(self):
        call = Call()
        call.apply_transition(CallState.INITIATED)
        assert call.initiated_at is not None

    def test_apply_transition_records_connected_at(self):
        call = Call(state=CallState.ANSWERED)
        call.apply_transition(CallState.CONNECTED)
        assert call.connected_at is not None

    def test_apply_transition_records_ended_at_for_terminal(self):
        call = Call()
        call.apply_transition(CallState.FAILED)
        assert call.ended_at is not None

    # ------------------------------------------------------------------
    # Idempotency (duplicate event_id)
    # ------------------------------------------------------------------

    def test_duplicate_event_id_is_ignored(self):
        """
        The same event arriving twice must not cause a second transition.
        This is the core idempotency guarantee.
        """
        call = Call()
        ok1 = call.apply_transition(CallState.RESERVED, event_id="evt-001")
        ok2 = call.apply_transition(CallState.INITIATED, event_id="evt-001")  # duplicate!

        assert ok1 is True
        assert ok2 is False          # rejected because event_id already seen
        assert call.state == CallState.RESERVED   # still at RESERVED, not INITIATED
        assert call.version == 1     # only one transition applied

    def test_different_event_ids_both_succeed(self):
        call = Call()
        ok1 = call.apply_transition(CallState.RESERVED, event_id="evt-A")
        ok2 = call.apply_transition(CallState.INITIATED, event_id="evt-B")
        assert ok1 is True
        assert ok2 is True
        assert call.state == CallState.INITIATED

    def test_processed_event_ids_accumulate(self):
        call = Call()
        call.apply_transition(CallState.RESERVED, event_id="e1")
        call.apply_transition(CallState.INITIATED, event_id="e2")
        assert "e1" in call.processed_event_ids
        assert "e2" in call.processed_event_ids

    def test_triple_duplicate_harmless(self):
        """
        Simulates Provider B sending ANSWERED three times.
        Only the first should apply.
        """
        call = Call(state=CallState.RINGING)
        results = [
            call.apply_transition(CallState.ANSWERED, event_id="dup-evt")
            for _ in range(3)
        ]
        assert results[0] is True   # first: accepted
        assert results[1] is False  # second: duplicate
        assert results[2] is False  # third: duplicate
        assert call.state == CallState.ANSWERED
        assert call.version == 1

    # ------------------------------------------------------------------
    # Out-of-order events
    # ------------------------------------------------------------------

    def test_out_of_order_completed_then_ringing_rejected(self):
        """
        Scenario: COMPLETED arrives first (early delivery), then RINGING
        arrives late.  The call must remain COMPLETED.
        """
        call = Call()
        call.apply_transition(CallState.COMPLETED, event_id="e-comp")
        assert call.state == CallState.COMPLETED

        # Now RINGING arrives late.
        ok = call.apply_transition(CallState.RINGING, event_id="e-ring")
        assert ok is False
        assert call.state == CallState.COMPLETED   # not resurrected

    def test_out_of_order_answered_after_completed_rejected(self):
        call = Call()
        call.apply_transition(CallState.COMPLETED, event_id="e1")
        ok = call.apply_transition(CallState.ANSWERED, event_id="e2")
        assert ok is False
        assert call.state == CallState.COMPLETED

    def test_backwards_state_ignored(self):
        """RINGING arriving after CONNECTED is silently dropped."""
        call = Call(state=CallState.CONNECTED)
        ok = call.apply_transition(CallState.RINGING, event_id="stale")
        assert ok is False
        assert call.state == CallState.CONNECTED

    # ------------------------------------------------------------------
    # Full lifecycle walkthrough
    # ------------------------------------------------------------------

    def test_full_happy_path_lifecycle(self):
        """Walk a call through the full happy path and check each state."""
        call = Call()
        transitions = [
            (CallState.RESERVED,  "e1"),
            (CallState.INITIATED, "e2"),
            (CallState.RINGING,   "e3"),
            (CallState.ANSWERED,  "e4"),
            (CallState.CONNECTED, "e5"),
            (CallState.COMPLETED, "e6"),
        ]
        for new_state, eid in transitions:
            ok = call.apply_transition(new_state, event_id=eid)
            assert ok is True, f"Expected transition to {new_state} to succeed"

        assert call.state == CallState.COMPLETED
        assert call.version == 6
        assert call.is_terminal() is True

    def test_repr_does_not_raise(self):
        call = Call()
        r = repr(call)
        assert "QUEUED" in r
