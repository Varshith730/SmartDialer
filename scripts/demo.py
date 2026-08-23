#!/usr/bin/env python
"""
scripts/demo.py
---------------
SmartDialer quick demo — runs in under 10 seconds.

Demonstrates the complete pipeline:
    PredictiveEngine → SafetyController → CallAllocator → ProviderA
                          ↓ (async events)
                      EventProcessor → StateStore
                          ↓ (periodic)
                      Reconciler

Run:
    python scripts/demo.py

Optional flags:
    --mode progressive    Run in progressive (1:1) mode instead of predictive.
    --provider b          Use Provider B (chaotic: duplicates, out-of-order).
    --agents N            Number of agents (default: 8).
    --borrowers N         Number of borrowers (default: 40).
    --cycles N            Number of dialling cycles (default: 8).
"""

import sys
import os

# Allow running from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import argparse
import logging

from app.models.campaign import DialMode
from app.simulation.runner import SimulationConfig, SimulationRunner


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SmartDialer Demo")
    p.add_argument("--mode", choices=["predictive", "progressive"],
                   default="predictive", help="Dialling mode")
    p.add_argument("--provider", choices=["a", "b"], default="a",
                   help="Telecom provider (a=reliable, b=chaotic)")
    p.add_argument("--agents", type=int, default=8, help="Number of agents")
    p.add_argument("--borrowers", type=int, default=40, help="Number of borrowers")
    p.add_argument("--cycles", type=int, default=8, help="Number of dialling cycles")
    p.add_argument("--answer-rate", type=float, default=0.60,
                   help="Simulated answer rate (0.0–1.0)")
    p.add_argument("--verbose", action="store_true", default=True)
    p.add_argument("--log-level", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level))

    config = SimulationConfig(
        campaign_name="Collections Q3 Demo",
        dial_mode=DialMode.PREDICTIVE if args.mode == "predictive" else DialMode.PROGRESSIVE,
        provider=f"provider_{args.provider}",
        n_agents=args.agents,
        n_borrowers=args.borrowers,
        answer_rate=args.answer_rate,

        # Very short delays so demo completes in seconds.
        ring_time=0.8,
        talk_time=1.5,
        delay_scale=0.08,
        cycle_interval=0.40,
        wrap_up_seconds=0.0,

        verbose=True,
    )

    runner = SimulationRunner(config)
    report = runner.run(n_cycles=args.cycles)

    # Print processor stats.
    proc_stats = runner.processor.stats()
    print(f"\n  Event Processor Stats:")
    print(f"    Events received : {proc_stats['total_received']}")
    print(f"    Events applied  : {proc_stats['total_applied']}")
    print(f"    Duplicates drop : {proc_stats['duplicates_dropped']}")
    print(f"    Out-of-order dr : {proc_stats['out_of_order_dropped']}")

    # Print SC decision breakdown.
    sc_summary = runner.safety_controller.decision_summary()
    print(f"\n  Safety Controller Decisions:")
    for decision_type, count in sc_summary.items():
        print(f"    {decision_type:<25}: {count}")

    # Print circuit breaker state.
    cb_stats = runner.circuit_breaker.stats()
    print(f"\n  Circuit Breaker [{cb_stats['provider']}]: {cb_stats['state']}")
    print(f"    Total failures  : {cb_stats['total_failures']}")
    print(f"    Total successes : {cb_stats['total_successes']}")
    print()


if __name__ == "__main__":
    main()
