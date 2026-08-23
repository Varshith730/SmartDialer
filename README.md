# SmartDialer

A fully functional predictive dialer prototype for a collections environment, built to demonstrate system design, concurrency correctness, and progressive/predictive pacing — without Kafka, Redis, Kubernetes, or microservices.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run all tests (202 tests, ~30s)
python -m pytest tests/ -q

# 3. Launch the Streamlit Control Center Dashboard
streamlit run frontend/app.py

# 4. Run High-Concurrency Load Test (100 / 1,000 / 10,000 agents)
python scripts/load_test.py

# 5. Run the live demo (predictive mode, ~5s)
python scripts/demo.py

# 6. Run the three-scenario comparison
python scripts/simulation.py
```

---

## Project Structure

```
SmartDialer/
├── app/
│   ├── models/
│   │   ├── agent.py         # Agent dataclass + AgentState enum
│   │   ├── borrower.py      # Borrower dataclass + priority/status enums
│   │   ├── call.py          # Call state machine (idempotency + ordering)
│   │   └── campaign.py      # Campaign config + DialMode enum
│   ├── repository/
│   │   └── state_store.py   # In-memory store with per-row locking
│   ├── dialer/
│   │   ├── allocator.py     # Executes one approved call (reserve→initiate)
│   │   ├── progressive.py   # 1-agent-to-1-call dialler
│   │   ├── predictive.py    # EMA-based pacing engine
│   │   └── reconciler.py    # Lease-based crash recovery
│   ├── safety/
│   │   ├── controller.py    # Final call-count authority (APPROVE/REDUCE/REJECT)
│   │   └── circuit_breaker.py  # CLOSED/OPEN/HALF_OPEN provider health tracking
│   ├── providers/
│   │   ├── interface.py     # TelecomProvider ABC + NullProvider
│   │   ├── provider_a.py    # Reliable provider (ordered events)
│   │   └── provider_b.py    # Chaotic provider (duplicates, out-of-order)
│   ├── events/
│   │   └── processor.py     # Routes provider events → call state transitions
│   └── simulation/
│       ├── scenarios.py     # Benchmark scenarios A, B, C, D
│       └── runner.py        # Full pipeline orchestrator with step/run modes
├── frontend/
│   ├── app.py               # Streamlit main entrypoint & operations dashboard
│   ├── state.py             # Shared simulation session state
│   ├── components/          # Reusable KPI cards, Plotly charts, styled tables
│   └── pages/
│       ├── 1_📊_Dashboard.py
│       ├── 2_👥_Agents.py
│       ├── 3_📞_Calls.py
│       ├── 4_📈_Pacing.py
│       ├── 5_🛡️_Safety.py
│       ├── 6_📡_Providers.py
│       ├── 7_⚠️_Failures.py
│       └── 8_🧪_Simulation.py
├── tests/
│   ├── test_agents.py           # Agent model + store (21 tests)
│   ├── test_calls.py            # Call state machine (24 tests)
│   ├── test_concurrency.py      # Race condition tests (6 tests)
│   ├── test_pacing.py           # Allocator + Progressive + Predictive (53 tests)
│   ├── test_provider_outage.py  # Circuit breaker + Safety Controller (33 tests)
│   ├── test_idempotency.py      # Duplicate event handling (19 tests)
│   ├── test_out_of_order_events.py  # Out-of-order events (18 tests)
│   ├── test_worker_crash.py     # Lease expiry + reconciler (25 tests)
│   └── test_end_to_end.py       # Full pipeline integration (15 tests)
├── scripts/
│   ├── demo.py              # Quick interactive CLI demo
│   ├── simulation.py        # Three-scenario comparison CLI
│   └── load_test.py         # High-concurrency load testing (10k agents)
└── docs/
    ├── architecture.md      # Pipeline, components, and Mermaid diagram
    ├── state_machines.md    # Call, Agent, Borrower, CircuitBreaker state models
    └── architecture_decision.md # Design decisions & technical interview defense
```

---

## Architecture

The system enforces a strict one-way pipeline:

```
Campaign
  ↓
PredictiveEngine          ← proposes N calls based on EMA answer rate
  ↓  requested_calls
SafetyController          ← APPROVES, REDUCES, REJECTS, or FALLBACK_PROGRESSIVE
  ↓  approved_count
CallAllocator             ← reserves agent + borrower, calls provider
  ↓  initiate_call()
TelecomProvider           ← delivers events asynchronously
  ↓  event_callback()
EventProcessor            ← applies state transitions (idempotent + ordered)
  ↓
StateStore                ← single source of truth for all entity state
```

**The Predictive Engine never talks to the provider.
The Safety Controller is never bypassed.**

See [`docs/architecture.md`](docs/architecture.md) for a full component breakdown.

---

## Key Design Properties

### Concurrency Correctness
- `StateStore` uses two-level locking: per-entity-type lock for bulk reads, per-row lock for atomic mutations.
- `atomic_reserve_agent()` and `atomic_reserve_borrower()` check-and-set within a single lock acquisition — no TOCTOU race.
- Equivalent PostgreSQL pattern: `UPDATE agents SET state='RESERVED' WHERE id=? AND state='AVAILABLE'` — check `rowcount == 1`.

### Idempotency (Invariant 4)
- Every `ProviderEvent` carries a unique `event_id`.
- `call.apply_transition(state, event_id)` stores processed IDs in `call.processed_event_ids`.
- Duplicate events with the same `event_id` are silently dropped.
- Provider B floods the system with 3× duplicates; the system stays consistent.

### Out-of-Order Events (Invariant 5)
- Each `CallState` has a rank (QUEUED=0 → COMPLETED=6).
- Backwards transitions (rank ≤ current rank) are rejected.
- Terminal states (COMPLETED, FAILED, CANCELLED) reject all further transitions — they are black holes.
- Provider B delivers `COMPLETED → ANSWERED → RINGING`; the call correctly ends in COMPLETED.

### Worker Crash Recovery (Invariant 6)
- Every reservation writes `lease_until = now + N seconds` on the agent and call.
- The `Reconciler` periodically scans for expired leases via `find_expired_reservations()`.
- Pre-provider crashes → call marked CANCELLED; post-provider crashes → FAILED.
- Live calls (CONNECTED/ANSWERED) are never killed by the reconciler.

### Provider Abstraction
- `TelecomProvider` is an abstract base class.
- The allocator depends only on `is_healthy()` and `initiate_call()`.
- Swapping providers requires zero changes to the allocator, safety controller, or pacing engine.

---

## Running Tests

```bash
# All tests, quiet
python -m pytest tests/ -q --timeout=30

# All tests, verbose
python -m pytest tests/ -v --timeout=30

# Specific phase
python -m pytest tests/test_calls.py tests/test_concurrency.py -v

# With deprecation warnings as errors (ensures Python 3.13 compatibility)
python -m pytest tests/ -v --timeout=30 -W error::DeprecationWarning
```

**Test counts by file:**

| File | Tests | What it covers |
|------|------:|----------------|
| `test_agents.py` | 21 | Agent model, state transitions, store operations |
| `test_calls.py` | 24 | Call state machine, idempotency, terminal states |
| `test_concurrency.py` | 6 | Race conditions, atomic reservation |
| `test_pacing.py` | 53 | Allocator, progressive dialler, predictive engine |
| `test_provider_outage.py` | 33 | Circuit breaker, safety controller decisions |
| `test_idempotency.py` | 19 | Duplicate event rejection at model + processor level |
| `test_out_of_order_events.py` | 18 | Backwards/scrambled events, terminal black hole |
| `test_worker_crash.py` | 25 | Lease expiry, reconciler, crash recovery |
| `test_end_to_end.py` | 15 | Full pipeline integration, all three modes |
| **Total** | **202** | |

---

## Demo Script Examples

```bash
# Default: 8 agents, 35 borrowers, predictive, Provider A, 8 cycles
python scripts/demo.py

# Progressive mode (1:1 ratio for comparison)
python scripts/demo.py --mode progressive

# Chaotic Provider B (duplicates, out-of-order events)
python scripts/demo.py --provider b --cycles 10

# Low answer rate (Safety Controller will REDUCE requests)
python scripts/demo.py --answer-rate 0.20 --agents 15 --borrowers 80

# Three-scenario comparison (takes ~30s)
python scripts/simulation.py
```

**Sample output:**
```
===================================================================================================================
  SmartDialer Simulation  |  Mode: predictive  |  Provider: provider_a  |  Agents: 8  |  Borrowers: 35
===================================================================================================================
Cycle  Available  Dialing  Connect    Wrap-Up  Inflight   Done   Fail   Cncl  AnswerRate           SC Decision
-------------------------------------------------------------------------------------------------------------------
    1          0        8        0          0        8      0      0      0       50.0%            APPROVE(8)
    2          5        3        0          0        3      5      3      0       69.3%            APPROVE(3)
    3          1        7        0          0        7      6      5      0       57.5%            APPROVE(7)
    4          6        2        0          0        2     12      6      0       80.7%            APPROVE(2)

============================================================
  Calls initiated: 35  |  Answered: 21  |  Answer rate: 60.0%
  EMA answer rate: 89.7%  |  Circuit Breaker: CLOSED
============================================================
```

---

## Design Decisions

See [`docs/decisions.md`](docs/decisions.md) for full Architecture Decision Records.

Key decisions:
- **In-memory store with interface abstraction**: Zero external dependencies; drop-in PostgreSQL replacement requires only implementing the `StateStore` interface.
- **Standard library only** (plus pytest): No Celery, Redis, Kafka, or Kubernetes.
- **EMA over ML**: Transparent, one-formula answer-rate estimation that a reviewer can trace by hand.
- **Safety Controller as hard boundary**: No code path from the pacing engine to the provider exists without passing through the controller.
- **Rank-ordered state machine**: Total ordering on `CallState` makes out-of-order detection O(1) per event.

---

## Requirements

- Python 3.11+
- pytest ≥ 8.0
- pytest-timeout ≥ 2.3

```bash
pip install -r requirements.txt
```

No other runtime dependencies.
