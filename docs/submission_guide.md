# SmartDialer — CredResolve Technical Submission Guide

## 1. Project Overview
- **Project Name:** SmartDialer — Autonomous Campaign, Predictive Pacing & Safety Engine
- **Target Role / Purpose:** CredResolve Technical Recruitment Assignment
- **Live Hosted Application:** [https://smartdialer-credresolve.streamlit.app/](https://smartdialer-credresolve.streamlit.app/)
- **GitHub Repository:** [https://github.com/Varshith730/SmartDialer](https://github.com/Varshith730/SmartDialer)

---

## 2. Quick Execution Guide (For Evaluators)

### 2.1 Run the Automated Test Suite (202 Tests)
```bash
# Run quiet suite (~25-35s)
python -m pytest tests/ -q

# Run verbose suite with test names
python -m pytest tests/ -v
```

### 2.2 Launch the Local Streamlit Control Center
```bash
python -m streamlit run frontend/app.py
```
*Opens at `http://localhost:8501`.*

### 2.3 Run High-Concurrency Load Testing (10k Agents)
```bash
python scripts/load_test.py
```

### 2.4 Run Terminal Live Demo & Multi-Scenario Benchmarks
```bash
# 5-second end-to-end interactive terminal demo
python scripts/demo.py

# 3-scenario comparison (Progressive vs Predictive vs Chaotic Carrier)
python scripts/simulation.py
```

---

## 3. Main Files to Inspect

| Component | File Path | Architectural Significance |
|---|---|---|
| **Linear Decision Flow** | [`app/simulation/runner.py`](file:///c:/PROJECTS/SmartDialer/app/simulation/runner.py) | Wires the entire pipeline: Pacing $\rightarrow$ Safety $\rightarrow$ Allocator $\rightarrow$ Provider. |
| **Safety Boundary** | [`app/safety/controller.py`](file:///c:/PROJECTS/SmartDialer/app/safety/controller.py) | Hard capacity bounding (`APPROVE`, `REDUCE`, `REJECT`, `FALLBACK_PROGRESSIVE`). |
| **Pacing Calculation** | [`app/dialer/predictive.py`](file:///c:/PROJECTS/SmartDialer/app/dialer/predictive.py) | EMA answer rate estimation and Erlang over-dialing proposal (zero provider references). |
| **Atomic Allocation** | [`app/dialer/allocator.py`](file:///c:/PROJECTS/SmartDialer/app/dialer/allocator.py) | 9-step atomic reservation and rollback pipeline. |
| **State Repository** | [`app/repository/state_store.py`](file:///c:/PROJECTS/SmartDialer/app/repository/state_store.py) | Thread-safe row-locking in-memory store with PostgreSQL equivalents documented. |
| **Idempotency & Ordering** | [`app/models/call.py`](file:///c:/PROJECTS/SmartDialer/app/models/call.py), [`app/events/processor.py`](file:///c:/PROJECTS/SmartDialer/app/events/processor.py) | $O(1)$ duplicate event rejection and monotonic rank ordering. |
| **Crash Recovery** | [`app/dialer/reconciler.py`](file:///c:/PROJECTS/SmartDialer/app/dialer/reconciler.py) | Out-of-band lease recovery protecting live calls. |
| **Carrier Simulation** | [`app/providers/provider_a.py`](file:///c:/PROJECTS/SmartDialer/app/providers/provider_a.py), [`app/providers/provider_b.py`](file:///c:/PROJECTS/SmartDialer/app/providers/provider_b.py) | Clean signaling vs. latency, drops, timeouts, and duplicates. |
| **Frontend Control Suite** | [`frontend/app.py`](file:///c:/PROJECTS/SmartDialer/frontend/app.py) | Streamlit live dashboard with top navigation and interactive inputs. |

---

## 4. Key Architectural Concepts Evaluators Should Know

1. **Prediction Proposes, Safety Decides, Allocation Executes:**
   - The `PredictiveEngine` only outputs an integer `requested_calls`. It has no knowledge of telecom providers and cannot trigger calls.
   - The `SafetyController` is the final authority that guarantees zero customer abandonment.
2. **Concurrency & Race Conditions:**
   - Evaluated using 50 concurrent threads racing on identical agents (`tests/test_concurrency.py`). Exactly one reservation succeeds; all others fail safely.
3. **Fault-Tolerance & Chaos:**
   - `Provider B` generates duplicate and out-of-order events. The `Call` state machine treats terminal states as black holes and drops duplicates via a `processed_event_ids` set.
4. **Lease-Based Worker Recovery:**
   - Allocations write an expiry lease (`lease_until`). If a worker crashes, the `Reconciler` detects the expired lease and recovers the agent without killing active (`CONNECTED`) conversations.

---

## 5. Known Limitations (Prototype vs. Production)

| Feature | Current Prototype | Production Roadmap |
|---|---|---|
| **Storage** | In-memory `StateStore` with `threading.Lock` | PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` |
| **Telephony** | Async daemon threads with simulated latency | SIP Trunk / Webhook integration (e.g. Twilio / Asterisk) |
| **Worker Scaling** | Single-process multi-threading | Distributed stateless worker processes polling queue |
| **Circuit State** | In-process atomic state | Centralized Redis / DB read-through cache |
| **Metrics** | In-memory list snapshots | Time-series database (Prometheus / Grafana) |
