"""
tests/test_worker_crash.py
---------------------------
Worker crash recovery tests.

These tests simulate the scenario where a dialler worker crashes after
reserving an agent and/or borrower but before completing the call setup.
The Reconciler must detect the expired lease and restore the system to
a consistent state.

Scenarios covered:
  1. Worker crashes after reserving agent (call stays RESERVED) → reconciler frees agent
  2. Worker crashes after initiating call (call stays INITIATED) → reconciler fails call + frees agent
  3. Borrower is released back to PENDING after crash (retry-eligible)
  4. Call is marked CANCELLED (pre-provider) or FAILED (post-initiation)
  5. Healthy calls (valid lease) are NOT touched by reconciler
  6. Terminal calls (already COMPLETED/FAILED) are skipped
  7. Live calls (CONNECTED/ANSWERED) are NOT killed even with expired lease
  8. Multiple simultaneous crashed workers: all are reconciled
  9. Reconciler stats track correctly
  10. Reconciler is idempotent: second run on same call does nothing
  11. Agent can be re-reserved after being released by reconciler

Design note on lease time:
    Tests use a very short lease (0.05 seconds) and sleep 0.1 seconds
    before running the reconciler, ensuring the lease is reliably expired
    without making tests slow.
"""

import time
import threading
import pytest

from app.dialer.allocator import AllocationRequest, CallAllocator
from app.dialer.reconciler import Reconciler, CLEANABLE_STATES
from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallState
from app.models.campaign import Campaign, CampaignStatus
from app.providers.interface import NullProvider
from app.repository.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SHORT_LEASE = 0.05      # seconds — expires fast in tests
WAIT_FOR_EXPIRY = 0.12  # seconds — safely past the short lease


def make_store() -> StateStore:
    return StateStore()


def make_agent_and_borrower(store: StateStore) -> tuple[Agent, Borrower]:
    agent = Agent(name="TestAgent")
    borrower = Borrower(name="TestBorrower", campaign_id="camp-1")
    store.save_agent(agent)
    store.save_borrower(borrower)
    return agent, borrower


def reserve_and_crash(
    store: StateStore,
    call_state_after_crash: CallState = CallState.INITIATED,
    lease_seconds: float = SHORT_LEASE,
) -> tuple[Agent, Borrower, Call]:
    """
    Simulate a worker that reserves resources, creates a call, then 'crashes'
    (i.e. we never advance the state further and the lease expires).

    Returns the agent, borrower, and call objects for assertion.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    agent, borrower = make_agent_and_borrower(store)
    reservation_id = str(uuid.uuid4())

    # Reserve agent and borrower (mimicking the allocator).
    store.atomic_reserve_agent(agent.id, reservation_id, lease_seconds=lease_seconds)
    store.atomic_reserve_borrower(borrower.id, reservation_id)

    # Create the call record.
    deadline = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
    call = Call(
        agent_id=agent.id,
        borrower_id=borrower.id,
        campaign_id="camp-1",
        reservation_id=reservation_id,
        lease_until=deadline,
    )
    call.apply_transition(CallState.RESERVED, event_id=f"{call.id}-reserved")

    # Walk call to the desired crash state.
    if call_state_after_crash == CallState.INITIATED:
        call.apply_transition(CallState.INITIATED, event_id=f"{call.id}-initiated")
    elif call_state_after_crash == CallState.RINGING:
        call.apply_transition(CallState.INITIATED, event_id=f"{call.id}-initiated")
        call.apply_transition(CallState.RINGING,   event_id=f"{call.id}-ringing")
    # RESERVED is the default (walk stopped above)

    store.save_call(call)

    # Also update agent to reflect the crash point.
    refreshed_agent = store.get_agent(agent.id)
    if call_state_after_crash in (CallState.INITIATED, CallState.RINGING):
        refreshed_agent.state = AgentState.DIALING
    store.save_agent(refreshed_agent)

    return agent, borrower, call


# ===========================================================================
# Basic reconciliation tests
# ===========================================================================

class TestReconcilerBasics:

    def test_no_expired_leases_returns_empty_result(self):
        store = make_store()
        reconciler = Reconciler(store)
        result = reconciler.run()
        assert result.total_expired == 0
        assert result.cleaned_up == 0
        assert not result.has_work()

    def test_finds_expired_call(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        reconciler = Reconciler(store)
        result = reconciler.run()
        assert result.total_expired == 1

    def test_run_returns_result_with_timestamp(self):
        store = make_store()
        reconciler = Reconciler(store)
        result = reconciler.run()
        assert result.run_at is not None

    def test_stats_track_runs(self):
        store = make_store()
        reconciler = Reconciler(store)
        reconciler.run()
        reconciler.run()
        assert reconciler.stats()["total_runs"] == 2


# ===========================================================================
# Crash after RESERVED state (pre-provider)
# ===========================================================================

class TestCrashBeforeProvider:
    """Worker crashes after reserving agent+borrower but before calling provider."""

    def test_agent_released_after_crash(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.RESERVED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        # Agent must be AVAILABLE again.
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

    def test_agent_reservation_cleared_after_crash(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.RESERVED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        refreshed = store.get_agent(agent.id)
        assert refreshed.reservation_id is None
        assert refreshed.lease_until is None

    def test_borrower_released_to_pending_after_crash(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.RESERVED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        refreshed = store.get_borrower(borrower.id)
        assert refreshed.status == BorrowerStatus.PENDING
        assert refreshed.reserved_by is None

    def test_call_marked_cancelled_after_crash(self):
        """Pre-provider crash → CANCELLED (never reached borrower's phone)."""
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.RESERVED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        refreshed = store.get_call(call.id)
        assert refreshed.state == CallState.CANCELLED

    def test_call_failure_reason_set(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.RESERVED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        refreshed = store.get_call(call.id)
        assert refreshed.failure_reason is not None
        assert "lease expired" in refreshed.failure_reason


# ===========================================================================
# Crash after INITIATED state (provider called, ringing)
# ===========================================================================

class TestCrashAfterProvider:
    """Worker crashes after provider accepted the call (INITIATED or RINGING)."""

    def test_agent_released_after_initiated_crash(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

    def test_borrower_released_after_initiated_crash(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        assert store.get_borrower(borrower.id).status == BorrowerStatus.PENDING

    def test_call_marked_failed_after_initiated_crash(self):
        """Post-provider crash → FAILED (provider was contacted)."""
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        refreshed = store.get_call(call.id)
        assert refreshed.state == CallState.FAILED

    def test_call_marked_failed_after_ringing_crash(self):
        """Crash during RINGING (borrower's phone was ringing) → FAILED."""
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.RINGING)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        refreshed = store.get_call(call.id)
        assert refreshed.state == CallState.FAILED


# ===========================================================================
# Healthy calls are not touched
# ===========================================================================

class TestHealthyCallsUntouched:

    def test_valid_lease_call_not_reconciled(self):
        """A call with a non-expired lease must not be touched."""
        store = make_store()
        # Use a long lease that won't expire during the test.
        agent, borrower, call = reserve_and_crash(
            store, CallState.INITIATED, lease_seconds=3600.0
        )

        reconciler = Reconciler(store)
        result = reconciler.run()

        assert result.total_expired == 0
        assert result.cleaned_up == 0
        # Agent must still be DIALING (not released).
        assert store.get_agent(agent.id).state == AgentState.DIALING

    def test_terminal_call_skipped(self):
        """
        Already COMPLETED/FAILED calls are not returned by find_expired_reservations()
        (which filters out terminal state calls), so the reconciler never sees them.
        """
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        # Pre-mark the call as FAILED (as if the event processor got there first).
        call.apply_transition(CallState.FAILED, event_id="pre-fail")
        store.save_call(call)

        reconciler = Reconciler(store)
        result = reconciler.run()

        # find_expired_reservations() excludes terminal-state calls entirely.
        # So the reconciler finds nothing to do.
        assert result.total_expired == 0
        assert result.cleaned_up == 0
        # Call is untouched (still FAILED as we left it).
        assert store.get_call(call.id).state == CallState.FAILED

    def test_connected_call_not_killed(self):
        """
        A CONNECTED call with an expired lease represents a live conversation.
        The reconciler must NEVER kill a live call.
        """
        store = make_store()
        agent = Agent(name="LiveAgent")
        borrower = Borrower(name="LiveBorrower")
        store.save_agent(agent)
        store.save_borrower(borrower)

        from datetime import datetime, timedelta, timezone
        expired = datetime.now(timezone.utc) - timedelta(seconds=60)
        call = Call(
            agent_id=agent.id,
            borrower_id=borrower.id,
            state=CallState.CONNECTED,
            lease_until=expired,     # already expired
        )
        store.save_call(call)

        reconciler = Reconciler(store)
        result = reconciler.run()

        assert result.live_calls_skipped == 1
        assert result.cleaned_up == 0
        # Call must still be CONNECTED.
        assert store.get_call(call.id).state == CallState.CONNECTED

    def test_answered_call_not_killed(self):
        """ANSWERED (borrower picked up) with expired lease must not be killed."""
        store = make_store()
        from datetime import datetime, timedelta, timezone
        expired = datetime.now(timezone.utc) - timedelta(seconds=60)
        call = Call(state=CallState.ANSWERED, lease_until=expired)
        store.save_call(call)

        result = Reconciler(store).run()

        assert result.live_calls_skipped == 1
        assert store.get_call(call.id).state == CallState.ANSWERED


# ===========================================================================
# Idempotency of reconciliation
# ===========================================================================

class TestReconcilerIdempotency:

    def test_second_run_does_not_double_release(self):
        """
        Running the reconciler twice on the same crashed call must be safe.
        The second run should see the call already terminal and skip it.
        """
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        reconciler = Reconciler(store)
        result1 = reconciler.run()
        result2 = reconciler.run()

        assert result1.cleaned_up == 1
        # Second run: call is now terminal, should be skipped.
        assert result2.cleaned_up == 0

    def test_agent_re_reservable_after_reconciliation(self):
        """
        After the reconciler releases an agent, that agent must be reservable
        again by the next allocation cycle.
        """
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE

        # Now try to reserve the agent for a new call.
        ok = store.atomic_reserve_agent(agent.id, "new-reservation-001")
        assert ok is True
        assert store.get_agent(agent.id).state == AgentState.RESERVED

    def test_borrower_re_dialable_after_reconciliation(self):
        """After release, the borrower must appear in list_dialable_borrowers()."""
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        Reconciler(store).run()

        dialable = store.list_dialable_borrowers("camp-1")
        assert any(b.id == borrower.id for b in dialable)


# ===========================================================================
# Multiple crashed workers
# ===========================================================================

class TestMultipleCrashedWorkers:

    def test_all_crashed_calls_reconciled(self):
        """Simulate N workers all crashing simultaneously."""
        N = 5
        store = make_store()

        crashed = []
        for _ in range(N):
            a, b, c = reserve_and_crash(store, CallState.INITIATED, SHORT_LEASE)
            crashed.append((a, b, c))

        time.sleep(WAIT_FOR_EXPIRY)

        result = Reconciler(store).run()

        assert result.total_expired == N
        assert result.cleaned_up == N

        # All agents should be AVAILABLE.
        for agent, borrower, call in crashed:
            assert store.get_agent(agent.id).state == AgentState.AVAILABLE
            assert store.get_borrower(borrower.id).status == BorrowerStatus.PENDING
            assert store.get_call(call.id).state == CallState.FAILED

    def test_concurrent_reconcilers_no_double_release(self):
        """
        Two reconciler threads running simultaneously on the same crashed call.
        Only one should perform the cleanup; the other should see it as terminal.
        """
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        results = []
        barrier = threading.Barrier(2)

        def run_reconciler():
            barrier.wait()
            r = Reconciler(store).run()
            results.append(r)

        t1 = threading.Thread(target=run_reconciler)
        t2 = threading.Thread(target=run_reconciler)
        t1.start(); t2.start()
        t1.join(); t2.join()

        total_cleaned = sum(r.cleaned_up for r in results)
        # Exactly one reconciler should have cleaned up.
        assert total_cleaned == 1, (
            f"Expected exactly 1 cleanup between 2 concurrent reconcilers, got {total_cleaned}"
        )

        # Agent must be AVAILABLE (not double-released).
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE


# ===========================================================================
# Reconciler details
# ===========================================================================

class TestReconcilerDetails:

    def test_result_repr_does_not_raise(self):
        from app.dialer.reconciler import ReconciliationResult
        r = ReconciliationResult(total_expired=3, cleaned_up=2)
        assert "expired=3" in repr(r)

    def test_details_list_contains_call_info(self):
        store = make_store()
        agent, borrower, call = reserve_and_crash(store, CallState.INITIATED)
        time.sleep(WAIT_FOR_EXPIRY)

        result = Reconciler(store).run()
        assert len(result.details) == 1
        assert call.id[:8] in result.details[0]

    def test_stats_cleaned_count_accumulates_across_runs(self):
        store = make_store()
        # Crash #1
        reserve_and_crash(store, CallState.INITIATED, SHORT_LEASE)
        time.sleep(WAIT_FOR_EXPIRY)
        reconciler = Reconciler(store)
        reconciler.run()

        # Crash #2 — fresh resources
        make_agent_and_borrower(store)
        a2, b2, c2 = reserve_and_crash(store, CallState.INITIATED, SHORT_LEASE)
        time.sleep(WAIT_FOR_EXPIRY)
        reconciler.run()

        assert reconciler.stats()["total_cleaned"] >= 2
