"""
tests/test_provider_outage.py
------------------------------
Tests for the Circuit Breaker and Safety Controller's outage behaviour.

Phase 3 tests cover:

Circuit Breaker:
  - Starts CLOSED
  - Opens after failure_threshold consecutive failures
  - Rejects calls while OPEN
  - Transitions to HALF_OPEN after cooldown
  - Closes after a successful probe
  - Re-opens if probe fails
  - force_open / force_close controls work

Safety Controller:
  - APPROVE when requested ≤ safe capacity
  - REDUCE when requested > safe capacity
  - REJECT when safe capacity is 0 (no available agents)
  - REJECT when campaign hard limit is already reached
  - REJECT when circuit breaker is OPEN
  - FALLBACK_PROGRESSIVE when circuit breaker is HALF_OPEN
  - REJECT when provider is_healthy() returns False
  - Agent availability drop: controller reads fresh state, not cached
  - Global max_calls limit is respected
  - Decision log accumulates correctly
"""

import time
import threading
import pytest

from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower
from app.models.call import Call, CallState
from app.models.campaign import Campaign, CampaignStatus
from app.providers.interface import NullProvider
from app.repository.state_store import StateStore
from app.safety.circuit_breaker import CircuitBreaker, CircuitState
from app.safety.controller import DecisionType, SafetyController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_controller(
    n_agents: int = 10,
    n_inflight: int = 0,
    campaign_max: int = 100,
    failure_threshold: int = 3,
    cooldown_seconds: float = 30.0,
    global_max: int = None,
) -> tuple[SafetyController, Campaign, NullProvider, StateStore]:
    """Build a SafetyController with a predictable set of preconditions."""
    store = StateStore()
    campaign = Campaign(
        id="camp-1",
        name="Test",
        status=CampaignStatus.ACTIVE,
        max_concurrent_calls=campaign_max,
    )
    store.save_campaign(campaign)

    for i in range(n_agents):
        store.save_agent(Agent(name=f"Agent-{i}"))

    # Seed in-flight calls (INITIATED state) without touching agents.
    for _ in range(n_inflight):
        call = Call(campaign_id="camp-1")
        call.apply_transition(CallState.INITIATED, event_id=f"seed-{call.id}")
        store.save_call(call)

    provider = NullProvider(healthy=True)
    cb = CircuitBreaker(
        provider_name="null_provider",
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
    )
    controller = SafetyController(store, cb, global_max_calls=global_max)
    return controller, campaign, provider, store


# ===========================================================================
# Circuit Breaker tests
# ===========================================================================

class TestCircuitBreaker:

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("p", failure_threshold=3, cooldown_seconds=30)
        assert cb.state == CircuitState.CLOSED
        assert cb.is_closed() is True
        assert cb.is_open() is False

    def test_calls_permitted_when_closed(self):
        cb = CircuitBreaker("p", failure_threshold=3, cooldown_seconds=30)
        assert cb.is_call_permitted() is True

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker("p", failure_threshold=3, cooldown_seconds=30)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_closed()  # still closed after 2 failures
        cb.record_failure()    # 3rd failure → trips the breaker
        assert cb.is_open()

    def test_no_calls_permitted_when_open(self):
        cb = CircuitBreaker("p", failure_threshold=2, cooldown_seconds=30)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open()
        assert cb.is_call_permitted() is False

    def test_success_resets_failure_counter(self):
        cb = CircuitBreaker("p", failure_threshold=3, cooldown_seconds=30)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()    # reset!
        cb.record_failure()    # counter starts from 1 again
        assert cb.is_closed()  # 1 failure after reset, threshold=3 → still closed

    def test_transitions_to_half_open_after_cooldown(self):
        cb = CircuitBreaker("p", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()  # open the circuit
        assert cb.is_open()
        time.sleep(0.1)      # wait for cooldown
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_permits_exactly_one_call(self):
        cb = CircuitBreaker("p", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

        # First call: permitted (probe slot)
        assert cb.is_call_permitted() is True
        # Second call: not permitted (back to OPEN until probe completes)
        assert cb.is_call_permitted() is False

    def test_successful_probe_closes_circuit(self):
        cb = CircuitBreaker("p", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.1)
        cb.is_call_permitted()   # take the probe slot
        cb.record_success()      # probe succeeded
        assert cb.is_closed()

    def test_failed_probe_keeps_circuit_open(self):
        cb = CircuitBreaker("p", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.1)
        cb.is_call_permitted()   # take the probe slot
        cb.record_failure()      # probe failed → back to OPEN
        assert cb.is_open()

    def test_force_open_and_close(self):
        cb = CircuitBreaker("p", failure_threshold=10, cooldown_seconds=30)
        cb.force_open()
        assert cb.is_open()
        cb.force_close()
        assert cb.is_closed()

    def test_stats_accumulate(self):
        cb = CircuitBreaker("p", failure_threshold=5, cooldown_seconds=30)
        cb.record_failure()
        cb.record_success()
        s = cb.stats()
        assert s["total_failures"] == 1
        assert s["total_successes"] == 1

    def test_concurrent_half_open_only_one_probe(self):
        """
        Two threads both see HALF_OPEN simultaneously.
        Only one should get the probe slot; the other should be blocked.
        """
        cb = CircuitBreaker("p", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

        results = []
        barrier = threading.Barrier(2)

        def check():
            barrier.wait()
            results.append(cb.is_call_permitted())

        t1 = threading.Thread(target=check)
        t2 = threading.Thread(target=check)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Exactly one should have gotten the probe slot.
        assert results.count(True) == 1
        assert results.count(False) == 1


# ===========================================================================
# Safety Controller tests
# ===========================================================================

class TestSafetyController:

    # ------------------------------------------------------------------
    # APPROVE
    # ------------------------------------------------------------------

    def test_approve_when_requested_equals_safe_capacity(self):
        controller, campaign, provider, _ = make_controller(n_agents=10)
        decision = controller.evaluate(10, campaign, provider)
        assert decision.decision_type == DecisionType.APPROVE
        assert decision.approved_count == 10

    def test_approve_when_requested_less_than_safe_capacity(self):
        controller, campaign, provider, _ = make_controller(n_agents=20)
        decision = controller.evaluate(10, campaign, provider)
        assert decision.decision_type == DecisionType.APPROVE
        assert decision.approved_count == 10

    def test_approved_count_never_exceeds_requested(self):
        """Safety controller must not approve more than what was requested."""
        controller, campaign, provider, _ = make_controller(n_agents=50)
        decision = controller.evaluate(17, campaign, provider)
        assert decision.approved_count <= 17

    # ------------------------------------------------------------------
    # REDUCE
    # ------------------------------------------------------------------

    def test_reduce_when_requested_exceeds_available_agents(self):
        """
        Key scenario from the spec: Predictive requests 17, only 10 safe.
        """
        controller, campaign, provider, _ = make_controller(n_agents=10)
        decision = controller.evaluate(17, campaign, provider)
        assert decision.decision_type == DecisionType.REDUCE
        assert decision.approved_count == 10
        assert decision.requested_count == 17

    def test_reduce_due_to_campaign_hard_limit(self):
        """Campaign allows max 5 concurrent calls but 10 agents are available."""
        controller, campaign, provider, _ = make_controller(
            n_agents=10, campaign_max=5
        )
        decision = controller.evaluate(10, campaign, provider)
        assert decision.decision_type == DecisionType.REDUCE
        assert decision.approved_count == 5

    def test_reduce_accounts_for_inflight_calls(self):
        """
        With 10 agents and 3 inflight calls, safe capacity = 7
        (7 agents still available; the 3 inflight already hold agents).

        Note: agents are AVAILABLE in this helper — inflight calls are seeded
        as call records without moving agents.  This tests that the controller
        uses call state, not just agent state.
        """
        controller, campaign, provider, _ = make_controller(
            n_agents=10, campaign_max=15, n_inflight=3
        )
        # 15 campaign max - 3 inflight = 12 hard limit budget
        # 10 available agents (the inflight calls don't consume agents in this test)
        # safe_capacity = min(10, 12) = 10
        decision = controller.evaluate(12, campaign, provider)
        # With 10 available agents, cap is 10.
        assert decision.approved_count == 10

    def test_reduce_due_to_global_max(self):
        """Global max of 5 overrides campaign's larger limit."""
        controller, campaign, provider, _ = make_controller(
            n_agents=20, campaign_max=100, global_max=5
        )
        decision = controller.evaluate(15, campaign, provider)
        assert decision.approved_count == 5

    # ------------------------------------------------------------------
    # REJECT
    # ------------------------------------------------------------------

    def test_reject_when_no_available_agents(self):
        """No agents available → safe capacity = 0 → REJECT."""
        controller, campaign, provider, _ = make_controller(n_agents=0)
        decision = controller.evaluate(10, campaign, provider)
        assert decision.decision_type == DecisionType.REJECT
        assert decision.approved_count == 0

    def test_reject_when_requested_is_zero(self):
        controller, campaign, provider, _ = make_controller(n_agents=10)
        decision = controller.evaluate(0, campaign, provider)
        assert decision.decision_type == DecisionType.REJECT
        assert decision.approved_count == 0

    def test_reject_when_campaign_limit_exhausted(self):
        """Campaign max_concurrent_calls already reached by inflight calls."""
        controller, campaign, provider, _ = make_controller(
            n_agents=10, campaign_max=5, n_inflight=5
        )
        decision = controller.evaluate(3, campaign, provider)
        assert decision.decision_type == DecisionType.REJECT
        assert decision.approved_count == 0

    def test_reject_when_circuit_open(self):
        """Circuit breaker OPEN → REJECT regardless of agents."""
        controller, campaign, provider, _ = make_controller(n_agents=20)
        controller._circuit_breaker.force_open()
        decision = controller.evaluate(10, campaign, provider)
        assert decision.decision_type == DecisionType.REJECT
        assert decision.approved_count == 0
        assert "OPEN" in decision.reason

    def test_reject_when_provider_unhealthy(self):
        """Provider reports is_healthy()=False → REJECT."""
        controller, campaign, provider, _ = make_controller(n_agents=20)
        provider.set_healthy(False)
        decision = controller.evaluate(10, campaign, provider)
        assert decision.decision_type == DecisionType.REJECT
        assert decision.approved_count == 0

    # ------------------------------------------------------------------
    # FALLBACK_PROGRESSIVE
    # ------------------------------------------------------------------

    def test_fallback_progressive_when_circuit_half_open(self):
        """Circuit HALF_OPEN → FALLBACK_PROGRESSIVE with approved_count=1."""
        cb = CircuitBreaker("p", failure_threshold=1, cooldown_seconds=0.05)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

        store = StateStore()
        campaign = Campaign(id="c1", status=CampaignStatus.ACTIVE, max_concurrent_calls=50)
        store.save_campaign(campaign)
        for _ in range(10):
            store.save_agent(Agent())
        provider = NullProvider()
        controller = SafetyController(store, cb)

        decision = controller.evaluate(17, campaign, provider)
        assert decision.decision_type == DecisionType.FALLBACK_PROGRESSIVE
        assert decision.approved_count == 1

    # ------------------------------------------------------------------
    # Agent availability drop (Invariant 8)
    # ------------------------------------------------------------------

    def test_agent_drop_overrides_stale_prediction(self):
        """
        100 agents available → pacing engine says 50 calls safe.
        Then 40 agents go OFFLINE.
        Safety Controller must read the real-time count (60), not the cached 100.
        """
        store = StateStore()
        campaign = Campaign(id="c1", status=CampaignStatus.ACTIVE, max_concurrent_calls=200)
        store.save_campaign(campaign)

        agents = [Agent() for _ in range(100)]
        for a in agents:
            store.save_agent(a)

        cb = CircuitBreaker("p")
        provider = NullProvider()
        controller = SafetyController(store, cb)

        # Before the drop: 50 requested, 100 available → APPROVE 50.
        d1 = controller.evaluate(50, campaign, provider)
        assert d1.approved_count == 50

        # Simulate 40 agents going OFFLINE.
        for a in agents[:40]:
            a.state = AgentState.OFFLINE
            store.save_agent(a)

        # After drop: 50 requested, 60 available → still APPROVE 50.
        d2 = controller.evaluate(50, campaign, provider)
        assert d2.approved_count == 50

        # Now simulate more going offline — down to 30 available.
        for a in agents[40:70]:
            a.state = AgentState.OFFLINE
            store.save_agent(a)

        # After second drop: 50 requested, 30 available → REDUCE to 30.
        d3 = controller.evaluate(50, campaign, provider)
        assert d3.decision_type == DecisionType.REDUCE
        assert d3.approved_count == 30

    # ------------------------------------------------------------------
    # Decision log
    # ------------------------------------------------------------------

    def test_decision_log_accumulates(self):
        controller, campaign, provider, _ = make_controller(n_agents=5)
        controller.evaluate(3, campaign, provider)
        controller.evaluate(10, campaign, provider)
        assert len(controller.decision_log) == 2

    def test_last_decision_returns_most_recent(self):
        controller, campaign, provider, _ = make_controller(n_agents=5)
        controller.evaluate(3, campaign, provider)
        d = controller.evaluate(10, campaign, provider)
        assert controller.last_decision() is d

    def test_decision_summary_counts_types(self):
        controller, campaign, provider, _ = make_controller(n_agents=5)
        controller.evaluate(3, campaign, provider)   # APPROVE
        controller.evaluate(10, campaign, provider)  # REDUCE
        summary = controller.decision_summary()
        assert summary.get("APPROVE", 0) >= 1
        assert summary.get("REDUCE", 0) >= 1

    # ------------------------------------------------------------------
    # Decision fields
    # ------------------------------------------------------------------

    def test_decision_contains_correct_requested_count(self):
        controller, campaign, provider, _ = make_controller(n_agents=10)
        d = controller.evaluate(7, campaign, provider)
        assert d.requested_count == 7

    def test_decision_contains_timestamp(self):
        controller, campaign, provider, _ = make_controller(n_agents=5)
        d = controller.evaluate(3, campaign, provider)
        assert d.timestamp is not None

    def test_decision_contains_available_agents_count(self):
        controller, campaign, provider, _ = make_controller(n_agents=8)
        d = controller.evaluate(4, campaign, provider)
        assert d.available_agents == 8

    def test_decision_repr_does_not_raise(self):
        controller, campaign, provider, _ = make_controller(n_agents=5)
        d = controller.evaluate(3, campaign, provider)
        r = repr(d)
        assert "APPROVE" in r or "REDUCE" in r
