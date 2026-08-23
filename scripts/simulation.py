#!/usr/bin/env python
"""
scripts/simulation.py
---------------------
SmartDialer comparative simulation.

Runs three scenarios back-to-back and prints a comparison table:
  1. Progressive mode with Provider A (baseline).
  2. Predictive mode with Provider A (main mode).
  3. Predictive mode with Provider B (chaotic provider, stress test).

Run:
    python scripts/simulation.py

Output:
    Per-cycle tables for each scenario + a final comparison summary.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
logging.basicConfig(level=logging.WARNING)

from app.models.campaign import DialMode
from app.simulation.runner import SimulationConfig, SimulationRunner


SHARED = dict(
    n_agents=10,
    n_borrowers=60,
    answer_rate=0.55,
    ring_time=0.8,
    talk_time=1.5,
    delay_scale=0.06,
    cycle_interval=0.35,
    wrap_up_seconds=0.0,
    cb_failure_threshold=3,
    cb_cooldown_seconds=2.0,
)

SCENARIOS = [
    ("1. Progressive + Provider A (baseline)",
     dict(dial_mode=DialMode.PROGRESSIVE, provider="provider_a")),
    ("2. Predictive  + Provider A",
     dict(dial_mode=DialMode.PREDICTIVE, provider="provider_a")),
    ("3. Predictive  + Provider B (chaotic)",
     dict(dial_mode=DialMode.PREDICTIVE, provider="provider_b")),
]


def run_all() -> None:
    reports = []
    for label, overrides in SCENARIOS:
        print(f"\n{'#' * 80}")
        print(f"# Scenario: {label}")
        print(f"{'#' * 80}")

        cfg = SimulationConfig(
            campaign_name=label,
            **{**SHARED, **overrides},
            verbose=True,
        )
        runner = SimulationRunner(cfg)
        report = runner.run(n_cycles=10)
        reports.append((label, report, runner))

    # ------------------------------------------------------------------
    # Comparison table
    # ------------------------------------------------------------------
    print("\n" + "=" * 90)
    print("  SCENARIO COMPARISON")
    print("=" * 90)
    print(f"  {'Scenario':<45}  {'Initiated':>9}  {'Answered':>8}  {'AnswerRate':>10}  {'Elapsed':>8}")
    print("-" * 90)
    for label, report, runner in reports:
        print(
            f"  {label:<45}"
            f"  {report.total_initiated:>9}"
            f"  {report.total_completed:>8}"
            f"  {report.observed_answer_rate:>10.1%}"
            f"  {report.elapsed_seconds:>7.1f}s"
        )
    print("=" * 90)

    # ------------------------------------------------------------------
    # Idempotency/ordering stats for Provider B
    # ------------------------------------------------------------------
    _, b_report, b_runner = reports[2]
    stats = b_runner.processor.stats()
    print(f"\n  Provider B idempotency/ordering stats:")
    print(f"    Events received     : {stats['total_received']}")
    print(f"    Events applied      : {stats['total_applied']}")
    print(f"    Duplicates dropped  : {stats['duplicates_dropped']}")
    print(f"    Out-of-order dropped: {stats['out_of_order_dropped']}")
    print()


if __name__ == "__main__":
    run_all()
