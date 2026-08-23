"""
app/simulation/runner.py
------------------------
Simulation Runner — orchestrates the full SmartDialer pipeline.

This ties together every component built in previous phases:

    Campaign → PredictiveEngine / ProgressiveDialer
           → SafetyController
           → CallAllocator
           → TelecomProvider  (events delivered asynchronously)
           → EventProcessor   (updates call/agent/borrower state)
           → Reconciler       (periodic crash recovery)

Purpose:
    The runner lets us exercise the real system under controlled conditions
    without needing a real phone network.  All timing is scaled down via
    the provider's delay_scale parameter.

Design:
    - Each "cycle" simulates one dialling decision interval.
    - The runner sleeps between cycles to let provider events arrive.
    - Metrics are captured after every cycle and returned in a final report.
    - No mocking — the same code paths used here are used in production.

Usage (Python):
    from app.simulation.runner import SimulationRunner, SimulationConfig
    runner = SimulationRunner(SimulationConfig(n_agents=10, n_borrowers=50))
    report = runner.run(n_cycles=10)
    print(report.summary())
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.dialer.allocator import AllocationRequest, CallAllocator
from app.dialer.predictive import PredictiveEngine
from app.dialer.progressive import ProgressiveDialer
from app.dialer.reconciler import Reconciler
from app.events.processor import EventProcessor
from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerPriority, BorrowerStatus
from app.models.call import CallState
from app.models.campaign import Campaign, CampaignStatus, DialMode
from app.providers.interface import TelecomProvider
from app.providers.provider_a import ProviderA
from app.providers.provider_b import ProviderB
from app.repository.state_store import StateStore
from app.safety.circuit_breaker import CircuitBreaker
from app.safety.controller import SafetyController, DecisionType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class SimulationConfig:
    """All parameters for a simulation run."""

    # Campaign
    campaign_id: str = "sim-campaign-001"
    campaign_name: str = "Simulation Campaign"
    max_concurrent_calls: int = 100
    max_calls_per_agent: float = 3.0
    dial_mode: DialMode = DialMode.PREDICTIVE

    # Agents
    n_agents: int = 10

    # Borrowers
    n_borrowers: int = 50
    high_priority_ratio: float = 0.2   # fraction with HIGH priority
    medium_priority_ratio: float = 0.5

    # Provider
    provider: str = "provider_a"          # "provider_a" or "provider_b"
    answer_rate: float = 0.60
    ring_time: float = 1.0               # seconds (scaled by delay_scale)
    talk_time: float = 2.0               # seconds (scaled by delay_scale)
    delay_scale: float = 0.05            # 0.05 → ~0.15s per full call lifecycle

    # Pacing engine (predictive mode)
    ema_alpha: float = 0.15
    initial_answer_rate: float = 0.50

    # Cycle timing
    cycle_interval: float = 0.35        # seconds between dialling cycles
    wrap_up_seconds: float = 0.0        # agent wrap-up time after call

    # Circuit breaker
    cb_failure_threshold: int = 3
    cb_cooldown_seconds: float = 5.0

    # Reconciler
    lease_seconds: float = 30.0

    # Reporting
    verbose: bool = True


# ---------------------------------------------------------------------------
# Per-cycle metrics
# ---------------------------------------------------------------------------

@dataclass
class CycleMetrics:
    """Snapshot of system state at the end of one dialling cycle."""

    cycle_num: int
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Dialling decision
    requested_calls: int = 0
    approved_calls: int = 0
    initiated_calls: int = 0
    sc_decision: str = ""

    # Agent counts
    agents_available: int = 0
    agents_reserved: int = 0
    agents_dialing: int = 0
    agents_connected: int = 0
    agents_wrap_up: int = 0
    agents_offline: int = 0

    # Call counts (cumulative)
    calls_initiated_total: int = 0
    calls_completed_total: int = 0
    calls_failed_total: int = 0
    calls_cancelled_total: int = 0
    calls_inflight: int = 0

    # Pacing engine
    smoothed_answer_rate: float = 0.0
    smoothed_talk_time: float = 0.0


# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------

@dataclass
class SimulationReport:
    """Aggregated results of a complete simulation run."""

    config: SimulationConfig
    cycles: list[CycleMetrics] = field(default_factory=list)
    run_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_seconds: float = 0.0

    @property
    def total_initiated(self) -> int:
        return self.cycles[-1].calls_initiated_total if self.cycles else 0

    @property
    def total_completed(self) -> int:
        return self.cycles[-1].calls_completed_total if self.cycles else 0

    @property
    def total_failed(self) -> int:
        return self.cycles[-1].calls_failed_total if self.cycles else 0

    @property
    def observed_answer_rate(self) -> float:
        answered = self.total_completed
        total = self.total_initiated
        return answered / total if total > 0 else 0.0

    def summary(self) -> str:
        """Return a human-readable summary of the simulation run."""
        lines = [
            "",
            "=" * 60,
            "  SIMULATION REPORT",
            "=" * 60,
            f"  Campaign       : {self.config.campaign_name}",
            f"  Mode           : {self.config.dial_mode.value}",
            f"  Provider       : {self.config.provider}",
            f"  Agents         : {self.config.n_agents}",
            f"  Borrowers      : {self.config.n_borrowers}",
            f"  Cycles         : {len(self.cycles)}",
            f"  Elapsed        : {self.elapsed_seconds:.2f}s",
            "-" * 60,
            f"  Calls initiated  : {self.total_initiated}",
            f"  Calls answered   : {self.total_completed}",
            f"  Calls failed     : {self.total_failed}",
            f"  Answer rate      : {self.observed_answer_rate:.1%}",
        ]
        if self.cycles:
            last = self.cycles[-1]
            lines += [
                f"  EMA answer rate  : {last.smoothed_answer_rate:.1%}",
                f"  EMA talk time    : {last.smoothed_talk_time:.1f}s",
            ]
        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class SimulationRunner:
    """
    Orchestrates a complete SmartDialer simulation.

    Build, configure, and run:
        runner = SimulationRunner(SimulationConfig(n_agents=10, n_borrowers=50))
        report = runner.run(n_cycles=10)
    """

    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.store = StateStore()

        # Build the campaign.
        self.campaign = Campaign(
            id=config.campaign_id,
            name=config.campaign_name,
            status=CampaignStatus.ACTIVE,
            max_concurrent_calls=config.max_concurrent_calls,
            max_calls_per_agent=config.max_calls_per_agent,
            dial_mode=config.dial_mode,
        )
        self.store.save_campaign(self.campaign)

        # Build agents.
        self.agents: list[Agent] = []
        for i in range(config.n_agents):
            a = Agent(name=f"Agent-{i+1:02d}")
            self.store.save_agent(a)
            self.agents.append(a)

        # Build borrowers with priority distribution.
        self.borrowers: list[Borrower] = []
        for i in range(config.n_borrowers):
            if i < int(config.n_borrowers * config.high_priority_ratio):
                priority = BorrowerPriority.HIGH
            elif i < int(config.n_borrowers * (config.high_priority_ratio + config.medium_priority_ratio)):
                priority = BorrowerPriority.MEDIUM
            else:
                priority = BorrowerPriority.LOW
            b = Borrower(
                name=f"Borrower-{i+1:03d}",
                campaign_id=config.campaign_id,
                priority=priority,
            )
            self.store.save_borrower(b)
            self.borrowers.append(b)

        # Build provider.
        if config.provider == "provider_b":
            self.provider: TelecomProvider = ProviderB(
                answer_rate=config.answer_rate,
                ring_time=config.ring_time,
                talk_time=config.talk_time,
                delay_scale=config.delay_scale,
                duplicate_probability=0.3,
                out_of_order_probability=0.2,
            )
        else:
            self.provider = ProviderA(
                answer_rate=config.answer_rate,
                ring_time=config.ring_time,
                talk_time=config.talk_time,
                delay_scale=config.delay_scale,
            )

        # Build infrastructure.
        self.circuit_breaker = CircuitBreaker(
            provider_name=self.provider.name,
            failure_threshold=config.cb_failure_threshold,
            cooldown_seconds=config.cb_cooldown_seconds,
        )
        self.pacing_engine = PredictiveEngine(
            store=self.store,
            alpha=config.ema_alpha,
            initial_answer_rate=config.initial_answer_rate,
        )
        self.safety_controller = SafetyController(
            store=self.store,
            circuit_breaker=self.circuit_breaker,
        )
        self.allocator = CallAllocator(self.store)
        self.processor = EventProcessor(
            store=self.store,
            pacing_engine=self.pacing_engine,
            wrap_up_seconds=config.wrap_up_seconds,
        )
        self.reconciler = Reconciler(self.store)

        # Progressive dialer (fallback / comparison mode).
        self.progressive_dialer = ProgressiveDialer(self.store, self.allocator)

        # UI step-mode counters (used when the frontend calls step() one cycle at a time)
        self._ui_cycle = 0
        self._ui_report = SimulationReport(config=self.config)

    # ------------------------------------------------------------------
    # Main run loop & step control
    # ------------------------------------------------------------------

    def step(self) -> CycleMetrics:
        """
        Run exactly one dialling cycle without sleeping.
        Used by the Streamlit frontend for interactive step-by-step simulation.
        Advances the internal cycle counter and appends metrics to self.report.
        """
        self._ui_cycle += 1
        metrics = self._run_cycle(self._ui_cycle)
        self._ui_report.cycles.append(metrics)
        self.reconciler.run()
        return metrics

    @property
    def cycle_count(self) -> int:
        """Total cycles run via step()."""
        return self._ui_cycle

    @property
    def report(self) -> SimulationReport:
        """Accumulated metrics from all step() calls."""
        return self._ui_report

    def set_answer_rate(self, rate: float) -> None:
        """Change the provider's answer rate at runtime (used by failure simulation)."""
        rate = max(0.0, min(1.0, rate))
        self.provider._answer_rate = rate

    def set_agents_offline(self, count: int) -> None:
        """
        Move `count` AVAILABLE agents to OFFLINE.
        Used by the failure simulation page to test Safety Controller
        response to sudden agent availability drops.
        """
        available = [a for a in self.store.list_agents() if a.state == AgentState.AVAILABLE]
        for agent in available[:count]:
            agent.state = AgentState.OFFLINE
            self.store.save_agent(agent)

    def restore_agents(self) -> None:
        """Bring all OFFLINE agents back to AVAILABLE."""
        for agent in self.store.list_agents():
            if agent.state == AgentState.OFFLINE:
                agent.state = AgentState.AVAILABLE
                self.store.save_agent(agent)

    def run(self, n_cycles: int = 10) -> SimulationReport:
        """
        Run `n_cycles` dialling cycles.

        Each cycle:
        1. Compute request (predictive) or derive slots (progressive).
        2. Safety Controller evaluates request.
        3. Allocator initiates approved calls → provider fires events async.
        4. Sleep to allow events to arrive.
        5. Reconciler scans for stale leases.
        6. Collect and print metrics.
        """
        report = SimulationReport(config=self.config)
        start = time.monotonic()

        if self.config.verbose:
            self._print_header()

        for cycle in range(1, n_cycles + 1):
            metrics = self._run_cycle(cycle)
            report.cycles.append(metrics)

            if self.config.verbose:
                self._print_cycle(metrics)

            time.sleep(self.config.cycle_interval)
            self.reconciler.run()

        report.elapsed_seconds = time.monotonic() - start

        if self.config.verbose:
            print(report.summary())

        return report

    # ------------------------------------------------------------------
    # Single cycle
    # ------------------------------------------------------------------

    def _run_cycle(self, cycle_num: int) -> CycleMetrics:
        metrics = CycleMetrics(cycle_num=cycle_num)

        is_predictive = (self.config.dial_mode == DialMode.PREDICTIVE)

        # ------------------------------------------------------------------
        # Step 1: Pacing decision.
        # ------------------------------------------------------------------
        if is_predictive:
            requested = self.pacing_engine.compute_request(self.campaign)
        else:
            # Progressive: 1 call per available agent.
            requested = len(self.store.list_available_agents())

        metrics.requested_calls = requested

        # ------------------------------------------------------------------
        # Step 2: Safety Controller.
        # ------------------------------------------------------------------
        decision = self.safety_controller.evaluate(
            requested, self.campaign, self.provider
        )
        metrics.approved_calls = decision.approved_count
        metrics.sc_decision = f"{decision.decision_type.value}({decision.approved_count})"

        # ------------------------------------------------------------------
        # Step 3: Build and execute allocation requests.
        # ------------------------------------------------------------------
        if decision.approved_count > 0:
            available_agents = self.store.list_available_agents()
            dialable_borrowers = self.store.list_dialable_borrowers(self.campaign.id)

            n_to_dial = min(
                decision.approved_count,
                len(available_agents),
                len(dialable_borrowers),
            )

            requests = [
                AllocationRequest(
                    agent_id=available_agents[i].id,
                    borrower_id=dialable_borrowers[i].id,
                    campaign_id=self.campaign.id,
                    provider_name=self.provider.name,
                    lease_seconds=self.config.lease_seconds,
                )
                for i in range(n_to_dial)
            ]

            # The provider's initiate_call() fires events in background threads.
            # The event_callback is processor.process — called from those threads.
            results = self.allocator.bulk_allocate(
                requests,
                self.provider,
                event_callback=self.processor.process,
            )

            succeeded = sum(1 for r in results if r.success)
            metrics.initiated_calls = succeeded

            # Update circuit breaker based on provider acceptance.
            for r in results:
                if r.success:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure()

        # ------------------------------------------------------------------
        # Step 4: Release any agents in WRAP_UP (if wrap_up_seconds == 0).
        # ------------------------------------------------------------------
        # When wrap_up_seconds is 0, agents should move immediately to AVAILABLE
        # after their call completes. We sweep through WRAP_UP agents here so
        # they are available for the next cycle.
        if self.config.wrap_up_seconds == 0.0:
            for agent in self.store.list_agents():
                from app.models.agent import AgentState as _AgentState
                if agent.state == _AgentState.WRAP_UP:
                    self.processor.complete_wrap_up(agent.id)

        # ------------------------------------------------------------------
        # Step 5: Collect metrics from current store state.
        # ------------------------------------------------------------------
        self._fill_metrics(metrics)
        return metrics

    def _fill_metrics(self, metrics: CycleMetrics) -> None:
        """Read current store state into metrics fields."""
        agents = self.store.list_agents()
        state_counts = {s: 0 for s in AgentState}
        for a in agents:
            state_counts[a.state] += 1

        metrics.agents_available = state_counts[AgentState.AVAILABLE]
        metrics.agents_reserved  = state_counts[AgentState.RESERVED]
        metrics.agents_dialing   = state_counts[AgentState.DIALING]
        metrics.agents_connected = state_counts[AgentState.CONNECTED]
        metrics.agents_wrap_up   = state_counts[AgentState.WRAP_UP]
        metrics.agents_offline   = state_counts[AgentState.OFFLINE]

        calls = self.store.list_calls(self.campaign.id)
        inflight_states = {CallState.INITIATED, CallState.RINGING, CallState.RESERVED,
                           CallState.ANSWERED, CallState.CONNECTED}
        metrics.calls_inflight = sum(1 for c in calls if c.state in inflight_states)
        metrics.calls_initiated_total  = len(calls)
        metrics.calls_completed_total  = sum(1 for c in calls if c.state == CallState.COMPLETED)
        metrics.calls_failed_total     = sum(1 for c in calls if c.state == CallState.FAILED)
        metrics.calls_cancelled_total  = sum(1 for c in calls if c.state == CallState.CANCELLED)

        snap = self.pacing_engine.last_snapshot()
        if snap:
            metrics.smoothed_answer_rate = snap.smoothed_answer_rate
            metrics.smoothed_talk_time   = snap.smoothed_talk_time

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    HEADER_FMT = (
        "{:>5}  {:>9}  {:>7}  {:>7}  {:>9}  {:>7}"
        "  {:>5}  {:>5}  {:>5}  {:>10}  {:>20}"
    )
    ROW_FMT = (
        "{:>5}  {:>9}  {:>7}  {:>7}  {:>9}  {:>7}"
        "  {:>5}  {:>5}  {:>5}  {:>10}  {:>20}"
    )

    def _print_header(self) -> None:
        print(f"\n{'='*115}")
        print(f"  SmartDialer Simulation  |  Mode: {self.config.dial_mode.value}"
              f"  |  Provider: {self.config.provider}"
              f"  |  Agents: {self.config.n_agents}"
              f"  |  Borrowers: {self.config.n_borrowers}")
        print(f"{'='*115}")
        print(self.HEADER_FMT.format(
            "Cycle", "Available", "Dialing", "Connect",
            "Wrap-Up", "Inflight",
            "Done", "Fail", "Cncl",
            "AnswerRate",
            "SC Decision",
        ))
        print("-" * 115)

    def _print_cycle(self, m: CycleMetrics) -> None:
        print(self.ROW_FMT.format(
            m.cycle_num,
            m.agents_available,
            m.agents_dialing,
            m.agents_connected,
            m.agents_wrap_up,
            m.calls_inflight,
            m.calls_completed_total,
            m.calls_failed_total,
            m.calls_cancelled_total,
            f"{m.smoothed_answer_rate:.1%}",
            m.sc_decision,
        ))
