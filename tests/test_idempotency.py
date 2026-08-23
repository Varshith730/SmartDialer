"""
tests/test_idempotency.py
--------------------------
Idempotency tests: duplicate provider events must be harmless.

Scenarios tested:
  1. Same event_id delivered twice → only one transition applied.
  2. ANSWERED delivered three times (Provider B style) → state changes once.
  3. COMPLETED delivered twice → still only one terminal transition.
  4. Event processor processes duplicate and returns False on second delivery.
  5. processed_event_ids accumulates correctly.
  6. Version does not increment on rejected duplicate.
  7. Duplicate events don't corrupt the pacing engine's answer rate.
  8. End-to-end: Provider B duplicate delivery leaves call consistent.
"""

import time
import threading
import pytest

from app.dialer.allocator import AllocationRequest, CallAllocator
from app.events.processor import EventProcessor
from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallState
from app.models.campaign import Campaign, CampaignStatus
from app.providers.interface import NullProvider, ProviderEvent
from app.providers.provider_b import ProviderB
from app.repository.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_call_in_store(store: StateStore, state: CallState = CallState.RINGING) -> Call:
    """Create a call already in `state` and save it to the store."""
    call = Call(agent_id="agent-001", borrower_id="borrower-001")
    # Walk the call to the desired state.
    transitions = [
        CallState.RESERVED, CallState.INITIATED, CallState.RINGING,
        CallState.ANSWERED, CallState.CONNECTED,
    ]
    for s in transitions:
        if s == state:
            call.apply_transition(s, event_id=f"setup-{s.value}")
            break
        call.apply_transition(s, event_id=f"setup-{s.value}")
    store.save_call(call)
    return call


def make_processor(store: StateStore = None) -> tuple[EventProcessor, StateStore]:
    if store is None:
        store = StateStore()
    processor = EventProcessor(store, wrap_up_seconds=0.0)
    return processor, store


# ===========================================================================
# Unit tests: Call model idempotency
# ===========================================================================

class TestCallModelIdempotency:
    """These tests work directly on the Call model without a store."""

    def test_same_event_id_applied_only_once(self):
        call = Call(state=CallState.RINGING)
        ok1 = call.apply_transition(CallState.ANSWERED, event_id="evt-001")
        ok2 = call.apply_transition(CallState.CONNECTED, event_id="evt-001")  # same id!

        assert ok1 is True
        assert ok2 is False              # rejected: duplicate event_id
        assert call.state == CallState.ANSWERED
        assert call.version == 1

    def test_triple_duplicate_answered(self):
        """Provider B style: ANSWERED ANSWERED ANSWERED."""
        call = Call(state=CallState.RINGING)
        results = [
            call.apply_transition(CallState.ANSWERED, event_id="dup-evt")
            for _ in range(3)
        ]
        assert results == [True, False, False]
        assert call.state == CallState.ANSWERED
        assert call.version == 1

    def test_duplicate_completed_is_harmless(self):
        call = Call(state=CallState.CONNECTED)
        ok1 = call.apply_transition(CallState.COMPLETED, event_id="comp-001")
        ok2 = call.apply_transition(CallState.COMPLETED, event_id="comp-001")

        assert ok1 is True
        assert ok2 is False
        assert call.state == CallState.COMPLETED
        assert call.version == 1

    def test_version_does_not_increment_on_duplicate(self):
        call = Call(state=CallState.INITIATED)
        call.apply_transition(CallState.RINGING, event_id="ring-001")
        version_after_first = call.version

        call.apply_transition(CallState.RINGING, event_id="ring-001")  # duplicate
        assert call.version == version_after_first  # unchanged

    def test_different_event_ids_each_advance_state(self):
        call = Call()
        call.apply_transition(CallState.RESERVED,  event_id="e1")
        call.apply_transition(CallState.INITIATED, event_id="e2")
        call.apply_transition(CallState.RINGING,   event_id="e3")
        call.apply_transition(CallState.ANSWERED,  event_id="e4")
        assert call.state == CallState.ANSWERED
        assert call.version == 4
        assert call.processed_event_ids == {"e1", "e2", "e3", "e4"}


# ===========================================================================
# Event Processor idempotency
# ===========================================================================

class TestEventProcessorIdempotency:

    def test_processor_returns_false_on_duplicate(self):
        processor, store = make_processor()
        call = make_call_in_store(store, CallState.RINGING)

        event = ProviderEvent(
            event_id="evt-A",
            call_id=call.id,
            event_type="ANSWERED",
        )

        result1 = processor.process(event)
        result2 = processor.process(event)   # same event object, same event_id

        assert result1 is True
        assert result2 is False

    def test_processor_state_unchanged_on_duplicate(self):
        processor, store = make_processor()
        call = make_call_in_store(store, CallState.RINGING)

        event = ProviderEvent(event_id="e1", call_id=call.id, event_type="ANSWERED")
        processor.process(event)
        processor.process(event)

        call_after = store.get_call(call.id)
        assert call_after.state == CallState.ANSWERED

    def test_triple_answered_leaves_call_in_answered(self):
        """Provider B ANSWERED ANSWERED ANSWERED → call stays ANSWERED."""
        store = StateStore()
        # Create call directly at RINGING without walk-up helper (clean version).
        call = Call(state=CallState.RINGING)
        store.save_call(call)
        processor = EventProcessor(store)

        for _ in range(3):
            processor.process(ProviderEvent(
                event_id="dup-answered",
                call_id=call.id,
                event_type="ANSWERED",
            ))

        call_after = store.get_call(call.id)
        assert call_after.state == CallState.ANSWERED
        assert call_after.version == 1   # only one transition applied

    def test_duplicate_completed_does_not_re_trigger_side_effects(self):
        """
        Duplicate COMPLETED must not double-update the borrower or
        double-release the agent.
        """
        store = StateStore()
        agent = Agent(name="AgentX")
        borrower = Borrower(name="BorrowerX")
        store.save_agent(agent)
        store.save_borrower(borrower)

        call = Call(
            agent_id=agent.id,
            borrower_id=borrower.id,
            state=CallState.CONNECTED,
        )
        store.save_call(call)

        processor = EventProcessor(store, wrap_up_seconds=0.0)

        # First COMPLETED → valid, agent goes to WRAP_UP, borrower COMPLETED.
        ok1 = processor.process(ProviderEvent(
            event_id="comp-dup", call_id=call.id, event_type="COMPLETED"
        ))
        assert ok1 is True
        assert store.get_agent(agent.id).state == AgentState.WRAP_UP
        assert store.get_borrower(borrower.id).status == BorrowerStatus.COMPLETED

        # Manually move agent to AVAILABLE (simulating wrap-up completion).
        processor.complete_wrap_up(agent.id)
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

        # Second COMPLETED → duplicate, must be dropped.
        ok2 = processor.process(ProviderEvent(
            event_id="comp-dup", call_id=call.id, event_type="COMPLETED"
        ))
        assert ok2 is False
        # Agent must NOT go back to WRAP_UP.
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

    def test_duplicate_metrics_tracked(self):
        processor, store = make_processor()
        call = make_call_in_store(store, CallState.RINGING)

        event = ProviderEvent(event_id="dup", call_id=call.id, event_type="ANSWERED")
        processor.process(event)
        processor.process(event)
        processor.process(event)

        stats = processor.stats()
        assert stats["duplicates_dropped"] == 2
        assert stats["total_applied"] == 1

    def test_unknown_call_id_tracked(self):
        processor, store = make_processor()
        processor.process(ProviderEvent(
            event_id="x", call_id="no-such-call", event_type="RINGING"
        ))
        assert processor.stats()["unknown_calls"] == 1

    # ------------------------------------------------------------------
    # Concurrent duplicate delivery
    # ------------------------------------------------------------------

    def test_concurrent_duplicate_events_idempotent(self):
        """
        10 threads all sending the same event simultaneously.
        Exactly 1 transition should succeed (version ends at 1).
        """
        store = StateStore()
        call = Call(state=CallState.RINGING)
        store.save_call(call)
        processor = EventProcessor(store)
        event = ProviderEvent(event_id="concurrent-dup", call_id=call.id, event_type="ANSWERED")

        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            results.append(processor.process(event))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()

        call_after = store.get_call(call.id)
        assert call_after.state == CallState.ANSWERED
        assert call_after.version == 1


# ===========================================================================
# End-to-end: Provider B duplicate delivery
# ===========================================================================

class TestProviderBDuplicateEndToEnd:
    """
    Use ProviderB's inject_duplicate to fire precise duplicate sequences
    and verify the call ends in a consistent state.
    """

    def test_provider_b_duplicate_answered_leaves_call_consistent(self):
        """ANSWERED sent 3 times → call state is ANSWERED, version=1."""
        store = StateStore()
        call = Call(state=CallState.RINGING)
        store.save_call(call)
        processor = EventProcessor(store)
        provider = ProviderB(delay_scale=0.0)

        shared_event_id = "b-answered-dup"
        provider.inject_duplicate(
            call_id=call.id,
            event_type="ANSWERED",
            event_id=shared_event_id,
            callback=processor.process,
            count=3,   # 3 copies, same event_id
        )

        call_after = store.get_call(call.id)
        assert call_after.state == CallState.ANSWERED
        assert call_after.version == 1

    def test_provider_b_duplicate_completed_leaves_call_terminal(self):
        """COMPLETED sent twice → call is COMPLETED exactly once."""
        store = StateStore()
        call = Call(state=CallState.CONNECTED)
        store.save_call(call)
        processor = EventProcessor(store)
        provider = ProviderB(delay_scale=0.0)

        provider.inject_duplicate(
            call_id=call.id,
            event_type="COMPLETED",
            event_id="b-comp-dup",
            callback=processor.process,
            count=2,
        )

        call_after = store.get_call(call.id)
        assert call_after.state == CallState.COMPLETED
        assert call_after.version == 1

    def test_pacing_engine_updated_once_despite_duplicates(self):
        """
        Even if COMPLETED arrives twice, the pacing engine should only
        be called once with answered=True.
        """
        store = StateStore()

        # Use a simple counter as a mock pacing engine.
        class MockEngine:
            def __init__(self):
                self.calls = []
            def record_call_outcome(self, answered, talk_duration_seconds=0.0):
                self.calls.append(answered)

        mock_engine = MockEngine()
        call = Call(state=CallState.CONNECTED)
        store.save_call(call)
        processor = EventProcessor(store, pacing_engine=mock_engine)
        provider = ProviderB(delay_scale=0.0)

        provider.inject_duplicate(
            call_id=call.id,
            event_type="COMPLETED",
            event_id="comp-once",
            callback=processor.process,
            count=3,
        )

        # Pacing engine should have been called exactly once.
        assert len(mock_engine.calls) == 1
        assert mock_engine.calls[0] is True  # answered=True
