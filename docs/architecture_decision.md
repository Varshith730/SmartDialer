# SmartDialer — Architecture Decision & Technical Defense Guide

This document answers the core system design questions and technical trade-offs for the SmartDialer architecture, designed to assist during technical defense.

---

## 1. Why Python & Standard Library?
- **Readability & Explainability:** Python allows explicit data modeling (dataclasses, enums) and transparent math calculations (EMA, capacity clamping) without framework noise.
- **Concurrency Primitives:** Python's standard `threading.Lock`, `threading.Barrier`, and `concurrent.futures.ThreadPoolExecutor` provide fine-grained thread synchronization to demonstrate race-condition safety directly in code.
- **Fast Test Execution:** The test suite (202 tests) runs in ~30s with zero external infrastructure setup.

---

## 2. Why Streamlit?
- **Observability Layer, Not Business Logic:** Streamlit is used purely to observe the internal state machine, charts, and metrics.
- **Direct Python Object Inspection:** Streamlit runs in the same Python runtime as the state store, allowing zero-overhead live inspections of dataclasses without serialization boilerplate.
- **Interactive Chaos Injection:** Control buttons on the frontend call real backend methods (e.g. `circuit_breaker.force_open()`, `reconciler.run()`) to demonstrate actual system reactions live.

---

## 3. Why an In-Memory Store for the Prototype?
- **Zero-Dependency Simplicity:** Avoids forcing Docker/Postgres installation on the reviewer's machine.
- **Clean Interface Contract:** `StateStore` implements atomic methods (`atomic_reserve_agent`, `atomic_reserve_borrower`, `find_expired_reservations`) with explicit SQL equivalents documented on each method.
- **Deterministic Race Condition Testing:** In-memory per-row locks allow creating 50-worker race conditions in unit tests that execute in milliseconds.

---

## 4. Why NOT Kafka, Redis, Celery, or Microservices?
- **Avoid Overengineering:** For a dialer prototype, adding message brokers and distributed coordinators obscures the core concurrency and safety logic.
- **Predictability & Explainability:** With synchronous/daemon thread architectures, the execution flow (`Prediction ➔ Safety ➔ Allocation ➔ Provider`) can be traced step-by-step in a single call stack.
- **Clear Failure Semantics:** In-process leases and atomic locks make crash recovery and idempotency easy to inspect and prove correct.

---

## 5. How Would PostgreSQL Be Introduced in Production?

Replacing `StateStore` with PostgreSQL requires zero changes to the dialer or safety engine:

| In-Memory Method | PostgreSQL Equivalent |
|------------------|----------------------|
| `atomic_reserve_agent(id, res_id, lease)` | `UPDATE agents SET state = 'RESERVED', reservation_id = :res_id, lease_until = :lease WHERE id = :id AND state = 'AVAILABLE';` (Check `rowcount == 1`) |
| `atomic_reserve_borrower(id, res_id)` | `UPDATE borrowers SET status = 'RESERVED', reserved_by = :res_id WHERE id = :id AND status = 'PENDING';` |
| `find_expired_reservations()` | `SELECT * FROM calls WHERE lease_until < NOW() AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') FOR UPDATE SKIP LOCKED;` |
| Bulk Borrower Selection | `SELECT * FROM borrowers WHERE status = 'PENDING' ORDER BY priority ASC, created_at ASC LIMIT :n FOR UPDATE SKIP LOCKED;` |

---

## 6. How Would the System Scale?

In a multi-worker production deployment:
1. **Stateless Dialer Workers:** Multiple worker processes poll the campaign queue and execute dial cycles.
2. **Database Row-Level Locking:** PostgreSQL's `SELECT ... FOR UPDATE SKIP LOCKED` allows hundreds of parallel dialer workers to acquire borrowers and agents without lock contention or deadlocks.
3. **Webhook Ingestion Layer:** Telecom provider callbacks (`RINGING`, `ANSWERED`, `COMPLETED`) hit a lightweight FastAPI/HTTP endpoint that writes to a `call_events` table with `UNIQUE(call_id, event_id)` for guaranteed idempotency.
4. **Distributed Lease Reconciler:** A periodic scheduled task (e.g. every 10s) runs the reconciliation query to release orphaned agent reservations from crashed workers.

---

## 7. What Are the Main Bottlenecks & Trade-Offs?

| Concern | Bottleneck in Prototype | Production Scaling Solution |
|---------|-------------------------|------------------------------|
| **Contention on Borrower Queue** | In-memory list scanning | `FOR UPDATE SKIP LOCKED` on borrower table partitioned by campaign. |
| **Provider HTTP Latency** | Mock daemon threads sleep | Asynchronous async HTTP client (e.g. `httpx` / `aiohttp`) with connection pooling. |
| **Circuit Breaker Synchronization** | Single-process memory | Centralized circuit state in Redis or DB read-through cache. |
| **Crash Recovery Latency** | Periodic reconciler scan interval | Short lease window (e.g. 5-15s) combined with event-driven crash alerts. |
| **Answer Rate Drift (Cold Start)** | Initial static estimate (50%) | Bayesian prior seeded from historical campaign performance data. |
