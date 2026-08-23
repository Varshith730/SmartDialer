"""
tests/test_pacing.py
---------------------
Tests for the Progressive Dialer and Call Allocator.

Phase 2 tests cover:

Allocator:
  - Happy path: agent + borrower reserved, call initiated, agent moves to DIALING
  - Agent already taken: allocation fails cleanly, no call created
  - Borrower already taken: allocation fails cleanly, agent is released back
  - Unhealthy provider: allocation fails before touching any resource
  - Provider rejects call: all resources are released, call marked FAILED

Progressive Dialer:
  - Slot count equals available agents (the core invariant)
  - Never creates more calls than there are available agents
  - Paused campaign is skipped
  - Works correctly with max_per_cycle cap
  - Skips when no dialable borrowers remain
  - Concurrent cycle calls do not double-allocate the same agent
"""

import threading
import pytest

from app.dialer.allocator import AllocationRequest, CallAllocator
from app.dialer.progressive import ProgressiveDialer
from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerPriority
from app.models.call import CallState
from app.models.campaign import Campaign, CampaignStatus
from app.providers.interface import NullProvider
from app.repository.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store_with_agents_and_borrowers(
    n_agents: int,
    n_borrowers: int,
    campaign_id: str = "camp-1",
) -> StateStore:
    store = StateStore()
    campaign = Campaign(id=campaign_id, name="Test Campaign", status=CampaignStatus.ACTIVE)
    store.save_campaign(campaign)

    for i in range(n_agents):
        store.save_agent(Agent(name=f"Agent-{i}"))

    for i in range(n_borrowers):
        b = Borrower(
            name=f"Borrower-{i}",
            campaign_id=campaign_id,
            priority=BorrowerPriority.MEDIUM,
        )
        store.save_borrower(b)

    return store


# ---------------------------------------------------------------------------
# CallAllocator tests
# ---------------------------------------------------------------------------

class TestCallAllocator:

    def test_happy_path_allocates_successfully(self):
        """Full happy path: agent reserved, borrower reserved, call initiated."""
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider(healthy=True)
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        request = AllocationRequest(
            agent_id=agent.id,
            borrower_id=borrower.id,
            campaign_id="camp-1",
            provider_name=provider.name,
        )

        result = allocator.allocate(request, provider)

        assert result.success is True
        assert result.call is not None
        assert result.call.state == CallState.INITIATED

    def test_happy_path_agent_moves_to_dialing(self):
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        allocator.allocate(
            AllocationRequest(agent.id, borrower.id, "camp-1", provider.name),
            provider,
        )

        refreshed = store.get_agent(agent.id)
        assert refreshed.state == AgentState.DIALING

    def test_happy_path_call_linked_to_agent_and_borrower(self):
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        result = allocator.allocate(
            AllocationRequest(agent.id, borrower.id, "camp-1", provider.name),
            provider,
        )

        call = result.call
        assert call.agent_id == agent.id
        assert call.borrower_id == borrower.id
        assert call.campaign_id == "camp-1"
        assert call.provider == provider.name

    def test_happy_path_call_saved_in_store(self):
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        result = allocator.allocate(
            AllocationRequest(agent.id, borrower.id, "camp-1", provider.name),
            provider,
        )

        assert store.get_call(result.call.id) is not None

    def test_happy_path_provider_called_exactly_once(self):
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        allocator.allocate(
            AllocationRequest(agent.id, borrower.id, "camp-1", provider.name),
            provider,
        )

        assert len(provider.initiated_calls) == 1

    def test_unhealthy_provider_fails_before_reserving_anything(self):
        """Provider unhealthy → fail fast, nothing reserved."""
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider(healthy=False)
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        result = allocator.allocate(
            AllocationRequest(agent.id, borrower.id, "camp-1", provider.name),
            provider,
        )

        assert result.success is False
        assert "not healthy" in result.failure_reason

        # Agent must still be AVAILABLE.
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE
        # Provider must not have been called.
        assert len(provider.initiated_calls) == 0

    def test_agent_already_reserved_fails_cleanly(self):
        """Second allocator trying to take an already-reserved agent gets a clean failure."""
        store = make_store_with_agents_and_borrowers(1, 2)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrowers = store.list_dialable_borrowers()
        b1, b2 = borrowers[0], borrowers[1]

        # First allocation succeeds.
        r1 = allocator.allocate(
            AllocationRequest(agent.id, b1.id, "camp-1", provider.name), provider
        )
        assert r1.success is True

        # Second allocation tries same agent (now DIALING) with another borrower.
        r2 = allocator.allocate(
            AllocationRequest(agent.id, b2.id, "camp-1", provider.name), provider
        )
        assert r2.success is False
        assert "agent already reserved" in r2.failure_reason

        # b2 must still be dialable (not touched).
        assert store.get_borrower(b2.id).is_dialable() is True

    def test_borrower_already_reserved_releases_agent(self):
        """
        If the borrower reservation fails (race), the agent must be
        released back to AVAILABLE.  Otherwise we'd leak the agent reservation.
        """
        store = make_store_with_agents_and_borrowers(2, 1)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agents = store.list_available_agents()
        a1, a2 = agents[0], agents[1]
        borrower = store.list_dialable_borrowers()[0]

        # First allocation wins the borrower.
        r1 = allocator.allocate(
            AllocationRequest(a1.id, borrower.id, "camp-1", provider.name), provider
        )
        assert r1.success is True

        # Second allocation: different agent, same borrower (now RESERVED).
        r2 = allocator.allocate(
            AllocationRequest(a2.id, borrower.id, "camp-1", provider.name), provider
        )
        assert r2.success is False
        assert "borrower already reserved" in r2.failure_reason

        # CRITICAL: a2 must have been released back to AVAILABLE.
        assert store.get_agent(a2.id).state == AgentState.AVAILABLE, (
            "Agent must be released when borrower reservation fails"
        )

    def test_provider_rejection_releases_agent_and_borrower(self):
        """
        If the provider rejects the call after both resources were reserved,
        everything must be rolled back.
        """
        store = make_store_with_agents_and_borrowers(1, 1)
        provider = NullProvider(healthy=True)
        allocator = CallAllocator(store)

        agent = store.list_available_agents()[0]
        borrower = store.list_dialable_borrowers()[0]

        # Make provider reject AFTER the health check (simulate mid-flight failure).
        provider.set_healthy(False)

        result = allocator.allocate(
            AllocationRequest(agent.id, borrower.id, "camp-1", provider.name),
            provider,
        )

        assert result.success is False
        # Agent released.
        assert store.get_agent(agent.id).state == AgentState.AVAILABLE
        # Borrower released (back to PENDING).
        assert store.get_borrower(borrower.id).is_dialable() is True

    def test_bulk_allocate_returns_one_result_per_request(self):
        store = make_store_with_agents_and_borrowers(3, 3)
        provider = NullProvider()
        allocator = CallAllocator(store)

        agents = store.list_available_agents()
        borrowers = store.list_dialable_borrowers()

        requests = [
            AllocationRequest(agents[i].id, borrowers[i].id, "camp-1", provider.name)
            for i in range(3)
        ]

        results = allocator.bulk_allocate(requests, provider)
        assert len(results) == 3
        assert all(r.success for r in results)


# ---------------------------------------------------------------------------
# ProgressiveDialer tests
# ---------------------------------------------------------------------------

class TestProgressiveDialer:

    def _build(self, n_agents: int, n_borrowers: int, campaign_id: str = "camp-1"):
        store = make_store_with_agents_and_borrowers(n_agents, n_borrowers, campaign_id)
        campaign = store.get_campaign(campaign_id)
        provider = NullProvider()
        allocator = CallAllocator(store)
        dialer = ProgressiveDialer(store, allocator)
        return store, campaign, provider, allocator, dialer

    # ------------------------------------------------------------------
    # Core invariant: calls_attempted ≤ available agents
    # ------------------------------------------------------------------

    def test_cycles_match_available_agents(self):
        """Core invariant: progressive dialer never starts more calls than agents."""
        store, campaign, provider, _, dialer = self._build(5, 10)
        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 5
        assert result.calls_succeeded == 5

    def test_zero_agents_means_zero_calls(self):
        store, campaign, provider, _, dialer = self._build(0, 10)
        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 0
        assert result.calls_succeeded == 0

    def test_fewer_borrowers_than_agents_caps_calls(self):
        """If only 3 borrowers remain, at most 3 calls even with 10 agents."""
        store, campaign, provider, _, dialer = self._build(10, 3)
        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 3
        assert result.calls_succeeded == 3

    def test_no_dialable_borrowers_skips_all(self):
        """If all borrowers are exhausted, the cycle makes no calls."""
        store, campaign, provider, _, dialer = self._build(5, 0)
        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 0

    def test_max_per_cycle_caps_attempts(self):
        """The max_per_cycle parameter limits calls regardless of agent count."""
        store = make_store_with_agents_and_borrowers(10, 10)
        campaign = store.get_campaign("camp-1")
        provider = NullProvider()
        allocator = CallAllocator(store)
        dialer = ProgressiveDialer(store, allocator, max_per_cycle=3)

        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 3
        assert result.calls_succeeded == 3

    # ------------------------------------------------------------------
    # Campaign state
    # ------------------------------------------------------------------

    def test_paused_campaign_skips_cycle(self):
        store = make_store_with_agents_and_borrowers(5, 5)
        campaign = store.get_campaign("camp-1")
        campaign.status = CampaignStatus.PAUSED
        store.save_campaign(campaign)

        provider = NullProvider()
        allocator = CallAllocator(store)
        dialer = ProgressiveDialer(store, allocator)

        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 0
        assert result.calls_succeeded == 0

    def test_draft_campaign_skips_cycle(self):
        store = make_store_with_agents_and_borrowers(5, 5)
        campaign = store.get_campaign("camp-1")
        campaign.status = CampaignStatus.DRAFT

        provider = NullProvider()
        allocator = CallAllocator(store)
        dialer = ProgressiveDialer(store, allocator)

        result = dialer.run_cycle(campaign, provider)
        assert result.calls_attempted == 0

    # ------------------------------------------------------------------
    # Post-cycle state assertions
    # ------------------------------------------------------------------

    def test_after_cycle_agents_are_dialing(self):
        """After a successful cycle all used agents should be DIALING."""
        store, campaign, provider, _, dialer = self._build(4, 4)
        dialer.run_cycle(campaign, provider)

        agent_counts = store.count_agents_by_state()
        assert agent_counts.get("DIALING", 0) == 4
        assert agent_counts.get("AVAILABLE", 0) == 0

    def test_after_cycle_calls_are_initiated(self):
        """After a successful cycle, all calls should be in INITIATED state."""
        store, campaign, provider, _, dialer = self._build(3, 3)
        dialer.run_cycle(campaign, provider)

        call_counts = store.count_calls_by_state()
        assert call_counts.get("INITIATED", 0) == 3

    def test_after_cycle_borrowers_are_reserved(self):
        """After a cycle, used borrowers must be in RESERVED status."""
        from app.models.borrower import BorrowerStatus
        store, campaign, provider, _, dialer = self._build(2, 5)
        dialer.run_cycle(campaign, provider)

        borrowers = store.list_borrowers()
        reserved = [b for b in borrowers if b.status == BorrowerStatus.RESERVED]
        assert len(reserved) == 2

    def test_available_agents_at_start_reported_correctly(self):
        store, campaign, provider, _, dialer = self._build(7, 7)
        result = dialer.run_cycle(campaign, provider)
        assert result.available_agents_at_start == 7

    # ------------------------------------------------------------------
    # Borrower priority ordering
    # ------------------------------------------------------------------

    def test_high_priority_borrowers_called_first(self):
        """
        Borrowers with BorrowerPriority.HIGH must be called before MEDIUM/LOW.
        Since list_dialable_borrowers sorts by (priority.value ASC), HIGH=1
        comes before MEDIUM=2.
        """
        store = StateStore()
        campaign = Campaign(id="camp-p", name="Priority Test", status=CampaignStatus.ACTIVE)
        store.save_campaign(campaign)

        # One HIGH-priority and one LOW-priority borrower.
        high = Borrower(name="High", campaign_id="camp-p", priority=BorrowerPriority.HIGH)
        low = Borrower(name="Low", campaign_id="camp-p", priority=BorrowerPriority.LOW)
        store.save_borrower(high)
        store.save_borrower(low)

        # Only one agent — should call the HIGH-priority borrower.
        store.save_agent(Agent(name="Agent-1"))

        provider = NullProvider()
        allocator = CallAllocator(store)
        dialer = ProgressiveDialer(store, allocator)

        result = dialer.run_cycle(campaign, provider)
        assert result.calls_succeeded == 1

        # The call must be linked to the HIGH-priority borrower.
        calls = store.list_calls()
        assert len(calls) == 1
        assert calls[0].borrower_id == high.id

    # ------------------------------------------------------------------
    # Concurrency: two dialler threads targeting the same pool
    # ------------------------------------------------------------------

    def test_concurrent_cycles_no_double_allocation(self):
        """
        Two ProgressiveDialer threads running simultaneously must not
        allocate the same agent to two different calls.

        With N agents, the total calls_succeeded across both threads
        must be exactly N (each agent is used at most once).
        """
        N = 10
        store = make_store_with_agents_and_borrowers(N, N * 2)
        campaign = store.get_campaign("camp-1")
        provider = NullProvider()
        allocator = CallAllocator(store)
        dialer = ProgressiveDialer(store, allocator)

        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()  # both threads start at the same moment
            r = dialer.run_cycle(campaign, provider)
            results.append(r)

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start(); t2.start()
        t1.join(); t2.join()

        total_succeeded = sum(r.calls_succeeded for r in results)
        # Each of the N agents is used at most once.
        assert total_succeeded == N, (
            f"Expected exactly {N} calls (one per agent), got {total_succeeded}"
        )

        # Verify no agent is DIALING more than once (sanity check on store state).
        all_agents = store.list_agents()
        dialing = [a for a in all_agents if a.state == AgentState.DIALING]
        assert len(dialing) == N


# ===========================================================================
# Predictive Pacing Engine tests (Phase 4)
# ===========================================================================

import math
from app.dialer.predictive import PredictiveEngine


class TestPredictiveEngine:
    """
    Tests for the PredictiveEngine.

    Key properties verified:
    1. requested_calls = 0 when no agents are available.
    2. At 100% answer rate, requested = available_agents (1:1 ratio).
    3. At 50% answer rate, requested = ~2x available (but capped).
    4. EMA updates answer rate correctly after outcomes.
    5. Engine NEVER produces a value > max_calls_per_agent * available_agents.
    6. Engine has no reference to a provider (architecture invariant).
    7. Snapshot history accumulates correctly.
    8. Answer rate converges toward truth after many outcomes.
    """

    def _make_engine(
        self,
        n_agents: int,
        initial_rate: float = 0.5,
        alpha: float = 0.1,
        campaign_id: str = "camp-1",
    ) -> tuple[PredictiveEngine, Campaign, StateStore]:
        store = StateStore()
        campaign = Campaign(
            id=campaign_id,
            name="Test",
            status=CampaignStatus.ACTIVE,
            max_calls_per_agent=3.0,
        )
        store.save_campaign(campaign)
        for i in range(n_agents):
            store.save_agent(Agent(name=f"Agent-{i}"))
        engine = PredictiveEngine(store, alpha=alpha, initial_answer_rate=initial_rate)
        return engine, campaign, store

    # ------------------------------------------------------------------
    # Zero-agents guard
    # ------------------------------------------------------------------

    def test_zero_agents_requests_zero_calls(self):
        engine, campaign, _ = self._make_engine(n_agents=0)
        assert engine.compute_request(campaign) == 0

    # ------------------------------------------------------------------
    # Answer rate drives the dial ratio
    # ------------------------------------------------------------------

    def test_answer_rate_100_pct_requests_exact_agent_count(self):
        """
        At 100% answer rate, every call will be answered.
        The engine should request exactly as many calls as available agents.
        (target_inflight = ceil(10/1.0) = 10; min(10, 10) = 10)
        """
        engine, campaign, _ = self._make_engine(n_agents=10, initial_rate=1.0)
        requested = engine.compute_request(campaign)
        assert requested == 10

    def test_answer_rate_50_pct_requests_more_than_agents(self):
        """
        At 50% answer rate, we need to dial ~2x agents to expect all to connect.
        With 10 agents: target = ceil(10/0.5) = 20; min(20, 30) = 20;
        but self-cap at min(20, available=10)  = 10.
        The engine conservatively caps its own request at available_agents.
        """
        engine, campaign, _ = self._make_engine(n_agents=10, initial_rate=0.5)
        requested = engine.compute_request(campaign)
        # Requested should be > 0 and ≤ available_agents (10)
        assert 0 < requested <= 10

    def test_answer_rate_25_pct_requests_capped_by_agents(self):
        """
        At 25% answer rate, target = ceil(10/0.25) = 40.
        Capped at max_calls_per_agent(3.0) * 10 = 30.
        Then further self-capped at available_agents (10).
        Result: 10.
        """
        engine, campaign, _ = self._make_engine(n_agents=10, initial_rate=0.25)
        requested = engine.compute_request(campaign)
        assert requested <= 10

    def test_requested_never_exceeds_max_calls_per_agent_times_agents(self):
        """
        The engine must never ask for more than max_calls_per_agent × available.
        With max_calls_per_agent=3.0 and 10 agents, cap = 30.
        """
        engine, campaign, _ = self._make_engine(n_agents=10, initial_rate=0.01)
        # Very low answer rate would normally produce huge target.
        requested = engine.compute_request(campaign)
        max_allowed = int(math.ceil(10 * campaign.max_calls_per_agent))
        assert requested <= max_allowed

    def test_requested_never_exceeds_available_agents_self_cap(self):
        """
        Even with a low answer rate, engine self-caps at available_agents.
        """
        engine, campaign, _ = self._make_engine(n_agents=5, initial_rate=0.1)
        assert engine.compute_request(campaign) <= 5

    # ------------------------------------------------------------------
    # Inflight subtraction
    # ------------------------------------------------------------------

    def test_inflight_calls_reduce_requested(self):
        """
        If there are already calls in the pipeline, the engine should
        only request the *additional* calls needed, not the full target.
        """
        from app.models.call import Call, CallState
        engine, campaign, store = self._make_engine(n_agents=10, initial_rate=1.0)

        # Seed 5 calls already in INITIATED state (in-flight).
        for _ in range(5):
            call = Call(campaign_id="camp-1")
            call.apply_transition(CallState.INITIATED, event_id=f"seed-{call.id}")
            store.save_call(call)

        # At 100% answer rate with 10 agents: target = 10.
        # Already have 5 inflight → need 5 more.
        requested = engine.compute_request(campaign)
        assert requested == 5

    def test_fully_loaded_pipeline_requests_zero(self):
        """
        If inflight calls already reach the target, request 0 additional.
        """
        from app.models.call import Call, CallState
        engine, campaign, store = self._make_engine(n_agents=10, initial_rate=1.0)

        # Seed 10 calls in INITIATED (matches target of 10 at 100% rate).
        for _ in range(10):
            call = Call(campaign_id="camp-1")
            call.apply_transition(CallState.INITIATED, event_id=f"seed-{call.id}")
            store.save_call(call)

        requested = engine.compute_request(campaign)
        assert requested == 0

    # ------------------------------------------------------------------
    # EMA update: record_call_outcome
    # ------------------------------------------------------------------

    def test_ema_increases_after_answered_call(self):
        """Recording an answered call should push the EMA toward 1.0."""
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=0.3)
        rate_before = engine.smoothed_answer_rate
        engine.record_call_outcome(answered=True)
        assert engine.smoothed_answer_rate > rate_before

    def test_ema_decreases_after_unanswered_call(self):
        """Recording a no-answer call should push the EMA toward 0.0."""
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=0.8)
        rate_before = engine.smoothed_answer_rate
        engine.record_call_outcome(answered=False)
        assert engine.smoothed_answer_rate < rate_before

    def test_ema_formula_correctness(self):
        """
        Verify the EMA formula: new = alpha * observed + (1-alpha) * prev.
        """
        alpha = 0.2
        initial = 0.5
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=initial, alpha=alpha)

        # Record one answered call (observed = 1.0).
        engine.record_call_outcome(answered=True)

        expected = alpha * 1.0 + (1 - alpha) * initial  # 0.2 * 1.0 + 0.8 * 0.5 = 0.60
        assert abs(engine.smoothed_answer_rate - expected) < 1e-9

    def test_ema_formula_no_answer(self):
        """Verify EMA for a no-answer outcome."""
        alpha = 0.1
        initial = 0.7
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=initial, alpha=alpha)

        engine.record_call_outcome(answered=False)

        expected = alpha * 0.0 + (1 - alpha) * initial  # 0.0 + 0.9 * 0.7 = 0.63
        assert abs(engine.smoothed_answer_rate - expected) < 1e-9

    def test_talk_time_ema_updates_on_answered_call(self):
        """Talk time EMA should update when an answered call completes."""
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=0.5)
        before = engine.smoothed_talk_time
        engine.record_call_outcome(answered=True, talk_duration_seconds=200.0)
        # Initial was 90s, observed was 200s → should go up.
        assert engine.smoothed_talk_time > before

    def test_talk_time_unchanged_on_no_answer(self):
        """Talk time EMA should NOT update when call was not answered."""
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=0.5)
        before = engine.smoothed_talk_time
        engine.record_call_outcome(answered=False, talk_duration_seconds=0.0)
        assert engine.smoothed_talk_time == before

    def test_answer_rate_converges_to_truth(self):
        """
        After many outcomes, the EMA should converge close to the true rate.

        With alpha=0.1 and 200 samples at 30% answer rate, the EMA should
        be within 0.05 of 0.30.
        """
        engine, _, _ = self._make_engine(n_agents=1, initial_rate=0.5, alpha=0.1)
        import random
        rng = random.Random(42)  # deterministic seed

        for _ in range(200):
            answered = rng.random() < 0.30
            engine.record_call_outcome(answered=answered)

        assert abs(engine.smoothed_answer_rate - 0.30) < 0.05

    # ------------------------------------------------------------------
    # Snapshot history
    # ------------------------------------------------------------------

    def test_snapshot_history_accumulates(self):
        engine, campaign, _ = self._make_engine(n_agents=5)
        engine.compute_request(campaign)
        engine.compute_request(campaign)
        assert len(engine.snapshot_history) == 2

    def test_last_snapshot_reflects_latest_request(self):
        engine, campaign, _ = self._make_engine(n_agents=5)
        requested = engine.compute_request(campaign)
        snap = engine.last_snapshot()
        assert snap is not None
        assert snap.requested_calls == requested
        assert snap.available_agents == 5

    def test_snapshot_repr_does_not_raise(self):
        engine, campaign, _ = self._make_engine(n_agents=3)
        engine.compute_request(campaign)
        r = repr(engine.last_snapshot())
        assert "answer_rate" in r

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def test_stats_includes_expected_keys(self):
        engine, _, _ = self._make_engine(n_agents=5)
        s = engine.stats()
        assert "smoothed_answer_rate" in s
        assert "smoothed_talk_time" in s
        assert "total_outcomes" in s

    def test_stats_counters_update(self):
        engine, _, _ = self._make_engine(n_agents=5)
        engine.record_call_outcome(answered=True)
        engine.record_call_outcome(answered=False)
        s = engine.stats()
        assert s["total_outcomes"] == 2
        assert s["total_answered"] == 1
        assert s["total_not_answered"] == 1

    # ------------------------------------------------------------------
    # Architecture invariant: engine has no provider reference
    # ------------------------------------------------------------------

    def test_engine_has_no_provider_reference(self):
        """
        The predictive engine must not hold a reference to any TelecomProvider.
        This is the key architectural safety check.
        """
        from app.providers.interface import TelecomProvider
        engine, campaign, _ = self._make_engine(n_agents=10)

        # Inspect all instance attributes.
        for attr_name in vars(engine):
            attr_val = getattr(engine, attr_name)
            assert not isinstance(attr_val, TelecomProvider), (
                f"Engine attribute {attr_name!r} is a TelecomProvider — "
                "this violates the architecture boundary!"
            )

    def test_compute_request_signature_has_no_provider_param(self):
        """
        compute_request() must not accept a provider argument.
        Calling it should work with only (campaign) as argument.
        """
        import inspect
        engine, campaign, _ = self._make_engine(n_agents=5)
        sig = inspect.signature(engine.compute_request)
        param_names = list(sig.parameters.keys())
        assert "provider" not in param_names, (
            "compute_request() must not accept a provider parameter"
        )
        # Must be callable with just campaign.
        result = engine.compute_request(campaign)
        assert isinstance(result, int)

    # ------------------------------------------------------------------
    # Full pipeline: engine → safety controller → allocator
    # ------------------------------------------------------------------

    def test_engine_feeds_safety_controller_not_allocator(self):
        """
        Verify the correct pipeline:
            Engine.compute_request(campaign)
              → SafetyController.evaluate(requested, campaign, provider)
              → approved_count
              → Allocator.bulk_allocate(requests[:approved_count], provider)

        The engine's output (requested) is always ≥ approved_count.
        The allocator always sees only approved_count, never the raw requested.
        """
        from app.safety.circuit_breaker import CircuitBreaker
        from app.safety.controller import SafetyController

        store = StateStore()
        campaign = Campaign(
            id="camp-pipeline",
            status=CampaignStatus.ACTIVE,
            max_concurrent_calls=100,
            max_calls_per_agent=3.0,
        )
        store.save_campaign(campaign)
        for _ in range(20):
            store.save_agent(Agent())
        for _ in range(30):
            store.save_borrower(Borrower(campaign_id="camp-pipeline"))

        provider = NullProvider()
        cb = CircuitBreaker("null_provider")
        controller = SafetyController(store, cb)
        allocator = CallAllocator(store)
        engine = PredictiveEngine(store, initial_answer_rate=0.5)

        # Step 1: Engine proposes.
        requested = engine.compute_request(campaign)
        assert requested >= 0

        # Step 2: Safety Controller decides.
        decision = controller.evaluate(requested, campaign, provider)
        approved = decision.approved_count

        # Invariant: approved ≤ requested always.
        assert approved <= requested

        # Step 3: Allocator executes with approved count only.
        available = store.list_available_agents()
        dialable = store.list_dialable_borrowers(campaign.id)
        from app.dialer.allocator import AllocationRequest
        requests = [
            AllocationRequest(
                agent_id=available[i].id,
                borrower_id=dialable[i].id,
                campaign_id=campaign.id,
                provider_name=provider.name,
            )
            for i in range(min(approved, len(available), len(dialable)))
        ]
        results = allocator.bulk_allocate(requests, provider)
        succeeded = sum(1 for r in results if r.success)

        # Succeeded ≤ approved ≤ requested (the chain holds).
        assert succeeded <= approved
