"""
app/simulation/scenarios.py
---------------------------
Predefined simulation scenarios for the SmartDialer system.

Scenarios:
  - Scenario A: Low answer rate (20%), talk time = 120s. Tests Safety Controller REDUCE behavior.
  - Scenario B: Normal conditions (50% answer rate, talk time = 90s). Balanced baseline.
  - Scenario C: High answer rate (70%, talk time = 180s). High agent utilization.
  - Scenario D: Changing conditions & chaos (good phase -> provider outage -> recovery).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from app.models.campaign import DialMode
from app.simulation.runner import SimulationConfig, SimulationReport, SimulationRunner


@dataclass
class Scenario:
    """Definition of a simulation scenario."""
    name: str
    description: str
    config: SimulationConfig
    n_cycles: int = 10


# ---------------------------------------------------------------------------
# Predefined Scenarios A, B, C
# ---------------------------------------------------------------------------

SCENARIO_A = Scenario(
    name="Scenario A — Low Answer Rate (Stress)",
    description="Answer rate = 20%, talk time = 120s. Tests aggressive pacing request and Safety Controller REDUCE throttling.",
    n_cycles=10,
    config=SimulationConfig(
        campaign_name="Scenario A: Low Answer Rate",
        dial_mode=DialMode.PREDICTIVE,
        provider="provider_a",
        n_agents=15,
        n_borrowers=80,
        answer_rate=0.20,
        talk_time=2.0,       # scaled down for simulation
        ring_time=0.8,
        delay_scale=0.05,
        cycle_interval=0.0,  # fast test/sim execution
        verbose=False,
    ),
)

SCENARIO_B = Scenario(
    name="Scenario B — Normal Balanced Operations",
    description="Answer rate = 50%, talk time = 90s. Balanced baseline where predictive engine converges to true answer rate.",
    n_cycles=10,
    config=SimulationConfig(
        campaign_name="Scenario B: Normal Operations",
        dial_mode=DialMode.PREDICTIVE,
        provider="provider_a",
        n_agents=20,
        n_borrowers=100,
        answer_rate=0.50,
        talk_time=1.5,
        ring_time=0.6,
        delay_scale=0.05,
        cycle_interval=0.0,
        verbose=False,
    ),
)

SCENARIO_C = Scenario(
    name="Scenario C — High Answer Rate (High Load)",
    description="Answer rate = 70%, talk time = 180s. High connect rate, agents stay busy in conversations, pacing self-regulates.",
    n_cycles=10,
    config=SimulationConfig(
        campaign_name="Scenario C: High Answer Rate",
        dial_mode=DialMode.PREDICTIVE,
        provider="provider_a",
        n_agents=20,
        n_borrowers=100,
        answer_rate=0.70,
        talk_time=2.5,
        ring_time=0.5,
        delay_scale=0.05,
        cycle_interval=0.0,
        verbose=False,
    ),
)


# ---------------------------------------------------------------------------
# Scenario Runners
# ---------------------------------------------------------------------------

def run_scenario(scenario: Scenario) -> Tuple[SimulationRunner, SimulationReport]:
    """
    Run Scenario A, B, or C to completion.
    Returns the runner instance and final report.
    """
    runner = SimulationRunner(scenario.config)
    report = runner.run(n_cycles=scenario.n_cycles)

    # Allow async provider threads a short window to settle
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        inflight = runner.store.list_calls(runner.campaign.id)
        active = [c for c in inflight if not c.is_terminal()]
        if not active:
            break
        time.sleep(0.05)

    return runner, report


def run_scenario_d() -> Tuple[SimulationRunner, SimulationReport, List[str]]:
    """
    Run Scenario D — Changing conditions and provider outage.
    Phases:
      1. Cycles 1-5: Normal operation (65% answer rate)
      2. Cycles 6-10: Provider outage injected (circuit breaker forced OPEN -> SC REJECTs)
      3. Cycles 11-15: Recovery phase (circuit breaker restored -> dialling resumes)

    Returns (runner, report, event_logs).
    """
    config = SimulationConfig(
        campaign_name="Scenario D: Dynamic Disruption & Recovery",
        dial_mode=DialMode.PREDICTIVE,
        provider="provider_a",
        n_agents=20,
        n_borrowers=150,
        answer_rate=0.65,
        ring_time=0.6,
        talk_time=1.5,
        delay_scale=0.05,
        cycle_interval=0.0,
        verbose=False,
    )

    runner = SimulationRunner(config)
    event_logs: List[str] = []

    # Phase 1: Normal dialling (Cycles 1-5)
    event_logs.append("Phase 1 started: Normal dialling under 65% answer rate.")
    for _ in range(5):
        runner.step()
    event_logs.append(f"Phase 1 complete: {runner.report.total_initiated} calls initiated so far.")

    # Phase 2: Provider Outage (Cycles 6-10)
    event_logs.append("Phase 2: Injected provider failure! Circuit breaker forced OPEN.")
    runner.circuit_breaker.force_open()
    for _ in range(5):
        runner.step()
    event_logs.append("Phase 2 complete: Safety Controller rejected all calls during provider outage.")

    # Phase 3: Recovery (Cycles 11-15)
    event_logs.append("Phase 3: Restoring provider health! Circuit breaker forced CLOSED.")
    runner.circuit_breaker.force_close()
    for _ in range(5):
        runner.step()
    event_logs.append(f"Phase 3 complete: Dialling recovered successfully. Total calls: {runner.report.total_initiated}.")

    return runner, runner.report, event_logs


ALL_SCENARIOS: Dict[str, Optional[Scenario]] = {
    "A": SCENARIO_A,
    "B": SCENARIO_B,
    "C": SCENARIO_C,
    "D": None,  # Special multi-phase execution
}
