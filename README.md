# SmartDialer — CredResolve Assignment

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://smartdialer-credresolve.streamlit.app/)
[![Tests](https://img.shields.io/badge/tests-202%20passed-success)](https://github.com/Varshith730/SmartDialer)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue)](https://www.python.org/)

> 🌐 **Live Hosted Application:** **[https://smartdialer-credresolve.streamlit.app/](https://smartdialer-credresolve.streamlit.app/)**  
> *The SmartDialer Operations Suite is deployed and running live on Streamlit Cloud. You can interact with the live dashboard, dispatch calls, tune pacing, and inject chaos without any local installation.*

---

## 1. Overview
SmartDialer is a production-grade functional prototype of an outbound progressive and predictive dialing system tailored for debt collection and high-touch contact environments. The system optimizes call center throughput by intelligently over-dialing based on empirical answer rates while enforcing strict safety constraints to prevent dropped calls or borrower abandonment.

The implementation prioritizes correctness over complexity: it demonstrates atomic concurrency, race-condition resistance, idempotent webhook processing, out-of-order event recovery, and carrier fault-tolerance without relying on heavy external infrastructure like Kafka, Redis, or microservices.

---

## 2. Key Architecture

The system enforces a strict linear decision and execution pipeline:

```
Predictive Pacing Engine  (Proposes volume based on EMA answer rate)
         │
         ▼
  Safety Controller       (Decides final capacity bounds: APPROVE / REDUCE / REJECT)
         │
         ▼
   Call Allocator         (Executes atomic agent + borrower reservation)
         │
         ▼
  Telecom Provider        (Carrier delivers async telephony signaling)
```

### Core Architecture Invariant:
- **Prediction proposes:** The `PredictiveEngine` estimates required calls using Exponential Moving Average (EMA). It holds **zero references** to telecom providers and cannot initiate calls.
- **Safety decides:** The `SafetyController` is the **final authority**. It evaluates live available agents, in-flight limits, provider health, and circuit breaker status to calculate a definitive safe bound.
- **Allocation executes:** The `CallAllocator` performs atomic 9-step reservation with lease deadlines.
- **Providers report events:** Telephony carriers (`ProviderA`, `ProviderB`) deliver asynchronous webhooks into the `EventProcessor`.

---

## 3. Features (Actually Implemented in Code)
- **Progressive Dialing Mode:** Strict 1:1 agent-to-call matching ensuring zero dropped calls.
- **Predictive Pacing Mode:** Statistical over-dialing utilizing Exponential Moving Average (EMA, $\alpha=0.15$) answer rate tracking.
- **Safety Controller Hard Boundary:** Evaluates all call requests and enforces real-time hard capacity clamping.
- **Atomic Concurrency & Row Locking:** Thread-safe in-memory `StateStore` with per-row locks preventing duplicate agent reservations under high multi-worker concurrency.
- **Deterministic Priority Borrower Selection:** Priority queue ordering (`HIGH=1 < MEDIUM=2 < LOW=3`) with FIFO tie-breaking within tiers.
- **Telecom Carrier Abstraction & Circuit Breaker:** Pluggable `TelecomProvider` ABC with automated 3-state `CircuitBreaker` (`CLOSED`, `OPEN`, `HALF_OPEN`).
- **Mock Carrier Implementations:**
  - `Provider A`: High-reliability carrier with ordered async events.
  - `Provider B`: Chaotic carrier with duplicate events, out-of-order deliveries, timeouts, and latency.
- **Idempotency Guard:** $O(1)$ duplicate webhook detection via unique `processed_event_ids` sets on call records.
- **Monotonic Rank Out-of-Order Handling:** Rejects stale or backwards transitions (e.g., late `ANSWERED` arriving after `COMPLETED`). Terminal states are black holes.
- **Lease-Based Worker Crash Recovery:** Background `Reconciler` detects expired worker leases, canceling uninitiated calls while protecting live `CONNECTED` conversations.
- **Interactive Streamlit Operations Suite:** 8-view dashboard with KPI metrics, Plotly visualizations, live campaign parameter tuning, and chaos failure injection buttons.
- **High-Concurrency Load Testing:** Automated benchmarking measuring throughput across 100, 1,000, and 10,000 entities (up to 44,000 ops/sec).

---

## 4. Technology Stack
- **Language & Runtime:** Python 3.11+ / Python 3.13
- **Test Framework:** `pytest` (202 test suite), `pytest-timeout`
- **Concurrency & Synchronization:** Standard Library (`threading.Lock`, `threading.Barrier`, `concurrent.futures.ThreadPoolExecutor`)
- **Frontend & Visualization:** `Streamlit`, `Plotly`, `Pandas`
- **Documentation & Reporting:** `ReportLab` (PDF generation), Markdown

---

## 5. Project Structure
```
SmartDialer/
├── app/
│   ├── models/
│   │   ├── agent.py               # 7-state Agent model + reservability logic
│   │   ├── borrower.py            # Priority-ordered Borrower model
│   │   ├── call.py                # 9-state Call machine (monotonic ranks + idempotency)
│   │   └── campaign.py            # Campaign settings + DialMode enum
│   ├── repository/
│   │   └── state_store.py         # Thread-safe in-memory store (per-row locking)
│   ├── dialer/
│   │   ├── allocator.py           # 9-step atomic allocation pipeline
│   │   ├── progressive.py         # 1:1 progressive dialer
│   │   ├── predictive.py          # EMA-based pacing engine
│   │   └── reconciler.py          # Lease-based worker crash recovery
│   ├── safety/
│   │   ├── controller.py          # Safety Controller (hard boundary)
│   │   └── circuit_breaker.py     # CLOSED -> OPEN -> HALF_OPEN breaker
│   ├── providers/
│   │   ├── interface.py           # TelecomProvider ABC + NullProvider
│   │   ├── provider_a.py          # Reliable provider (ordered events)
│   │   └── provider_b.py          # Chaotic provider (duplicates, out-of-order)
│   ├── events/
│   │   └── processor.py           # Ingestion router & terminal side-effects
│   └── simulation/
│       ├── scenarios.py           # Benchmark scenarios A, B, C, D
│       └── runner.py              # Full pipeline orchestrator with step/run modes
├── frontend/
│   ├── app.py                     # Streamlit main entrypoint & operations dashboard
│   ├── state.py                   # Shared simulation session state
│   ├── components/                # Reusable KPI cards, Plotly charts, styled tables
│   │   ├── metrics.py
│   │   ├── charts.py
│   │   └── tables.py
│   └── pages/                     # Multi-page views (Dashboard, Agents, Calls, etc.)
├── tests/                         # 9 test suites (202 unit & integration tests)
├── scripts/
│   ├── demo.py                    # Quick interactive CLI demo (~5s)
│   ├── simulation.py              # Three-scenario comparison CLI
│   ├── load_test.py               # High-concurrency load testing (10k agents)
│   └── generate_pdfs.py           # Technical PDF documentation generator
├── docs/
│   ├── architecture.md            # Pipeline architecture with Mermaid diagram
│   ├── state_machines.md          # State transition diagrams & invariants
│   ├── architecture_decision.md   # Design decisions & technical defense answers
│   └── submission_guide.md        # CredResolve evaluation guide
└── screenshots/                   # Application dashboard screenshots
```

---

## 6. Installation

```bash
# 1. Clone the repository
git clone https://github.com/Varshith730/SmartDialer.git
cd SmartDialer

# 2. Create and activate a virtual environment (optional but recommended)
python -m venv venv

# Windows:
venv\Scripts\activate

# Linux / macOS:
source venv/bin/activate

# 3. Install required dependencies
pip install -r requirements.txt
```

---

## 7. Run Application

To start the interactive **SmartDialer Operations Suite** dashboard:

```bash
python -m streamlit run frontend/app.py
```
Open your browser at `http://localhost:8501`.

---

## 8. Run Tests

Execute the automated test suite (202 tests across 9 test files):

```bash
# Run quiet test suite (~25-35 seconds)
python -m pytest tests/ -q

# Run verbose suite with individual test names
python -m pytest tests/ -v
```

---

## 9. Run Simulation

Execute the multi-scenario comparative simulation:

```bash
python scripts/simulation.py
```
*Runs Progressive vs. Predictive vs. Chaotic Provider B side-by-side with full metric tables.*

---

## 10. Run Load Test

Execute high-concurrency throughput benchmarks across 100, 1,000, and 10,000 agents:

```bash
python scripts/load_test.py
```

---

## 11. Live Demo

The application is deployed and publicly accessible:  
👉 **[https://smartdialer-credresolve.streamlit.app/](https://smartdialer-credresolve.streamlit.app/)**

---

## 12. Failure Handling

SmartDialer handles failure conditions at every layer:
1. **Carrier Outages:** When consecutive provider failures exceed threshold (default: 5), `CircuitBreaker` trips to `OPEN`. The `SafetyController` immediately blocks all new calls without killing active conversations. After a cooldown, a single probe is admitted in `HALF_OPEN` state.
2. **Worker Server Crashes:** If an allocation worker crashes between reserving an agent and initiating a call, its lease expires. The background `Reconciler` discovers the expired lease and safely returns the agent to `AVAILABLE`.
3. **Shift End / Sudden Agent Drop:** If agents suddenly disconnect or move `OFFLINE`, the `SafetyController` instantly recalculates capacity against live available agents, refusing to trust stale predictions.

---

## 13. Concurrency

SmartDialer implements a two-level locking model:
- **Entity-Level Locks:** Protect snapshot queries and list operations.
- **Row-Level Locks:** Dedicated `threading.Lock` per agent and borrower ID for atomic check-and-set operations (`atomic_reserve_agent`, `atomic_reserve_borrower`).
- **Race Condition Safety:** Verified in `tests/test_concurrency.py` where 50 concurrent worker threads attempt to reserve the exact same agent simultaneously; exactly 1 worker succeeds and 49 fail safely.

In PostgreSQL, this translates directly to:
```sql
UPDATE agents SET state = 'RESERVED', reservation_id = :res_id, lease_until = :lease
WHERE id = :id AND state = 'AVAILABLE';
```

---

## 14. Idempotency

Telephony carriers often resend webhooks on network timeouts. In SmartDialer:
- Every provider event contains a globally unique `event_id`.
- Each `Call` model maintains a `processed_event_ids: set[str]`.
- Incoming events check set membership in $O(1)$. If `event_id` is already present, the transition is dropped immediately before any version counters or state transitions mutate.

---

## 15. Out-of-Order Events

Due to network routing, a `COMPLETED` webhook may arrive before a delayed `RINGING` or `ANSWERED` event.
- Every state in `CallState` is assigned a monotonic rank:
  $$\text{QUEUED}(0) < \text{RESERVED}(1) < \text{INITIATED}(2) < \text{RINGING}(3) < \text{ANSWERED}(4) < \text{CONNECTED}(5) < \text{TERMINAL}(6)$$
- `apply_transition()` accepts an event only if $\text{rank}(\text{new\_state}) > \text{rank}(\text{current\_state})$.
- Terminal states (`COMPLETED`, `FAILED`, `CANCELLED`) all share rank 6, acting as black holes. A late `ANSWERED` event cannot resurrect a completed call.

---

## 16. Scaling Considerations (Prototype vs. Production)

| Architecture Component | Current Prototype Implementation | Production Scaling Upgrade |
|---|---|---|
| **State Repository** | In-Memory `StateStore` with `threading.Lock` | PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` |
| **Telephony Transport** | Async daemon threads with simulated latency | Asynchronous HTTP client (`httpx`) to Twilio / SIP Trunk |
| **Worker Scaling** | Single-process multi-threading | Stateless worker pool (Kubernetes / ECS) polling campaign queues |
| **Circuit Breaker State** | In-process atomic lock | Distributed Redis read-through cache |
| **Telemetry & Metrics** | In-memory cycle history | Prometheus metrics + Grafana dashboard |
