# Architecture Decision Records

This document records the key design decisions made during the SmartDialer build, the alternatives considered, and the reasoning for each choice.

---

## ADR-001: In-Memory Repository with Interface Abstraction

**Status:** Accepted

**Context:**
The assignment explicitly prohibits Docker, Redis, Kafka, and microservices. The prototype must be runnable with a single `python -m pytest` command. However, the repository layer should be replaceable with PostgreSQL without touching any business logic.

**Decision:**
Implement `StateStore` as an in-memory dict store with a public interface that mirrors SQL semantics exactly. Each method has a documented SQL equivalent in its docstring.

**Consequences:**
- Zero external dependencies at runtime.
- Thread safety is provided by `threading.Lock` per row, equivalent to `SELECT FOR UPDATE` in PostgreSQL.
- Replacing with PostgreSQL requires implementing the same method signatures on a new class backed by SQLAlchemy. All callers (allocator, reconciler, safety controller) are unchanged.
- The per-row lock pattern is explicitly documented in `state_store.py` as the intended upgrade path.

**Alternatives Considered:**
- SQLite — adds a dependency and complicates test isolation.
- Postgres in Docker — violates the "no Docker" constraint and makes CI harder.

---

## ADR-002: Safety Controller as Hard Architectural Boundary

**Status:** Accepted

**Context:**
The assignment states: *"The Safety Controller must be the final authority. There must be NO code path allowing the predictive engine to bypass the Safety Controller."*

**Decision:**
The `PredictiveEngine` holds no reference to `TelecomProvider`, `CallAllocator`, or `SafetyController`. Its only output is an `int` (requested count). The only way to start a call is:

```
PredictiveEngine.compute_request() → int
    → SafetyController.evaluate() → SafetyDecision
    → CallAllocator.bulk_allocate() → [AllocationResult]
    → TelecomProvider.initiate_call()
```

This is enforced in two ways:
1. **By construction:** `PredictiveEngine.__init__` takes only `store: StateStore`. No provider is injected.
2. **By test:** `test_engine_has_no_provider_reference` inspects the engine's instance vars and fails if any provider-like attribute is found. `test_compute_request_signature_has_no_provider_param` inspects the signature.

**Consequences:**
- Reviewers can verify the invariant by reading the `__init__` signature.
- The test suite will catch any future accidental injection.

---

## ADR-003: Rank-Ordered Call State Machine

**Status:** Accepted

**Context:**
Provider B delivers events out of order: `COMPLETED` can arrive before `RINGING`. We need to reject backwards transitions without a complex per-state transition table.

**Decision:**
Define a total order on `CallState` values via a `STATE_ORDER` list. `state_rank(s)` returns the list index. `apply_transition(new_state)` accepts only if `rank(new_state) > rank(current_state)`.

Terminal states (`COMPLETED`, `FAILED`, `CANCELLED`) all have rank 6 — equal to each other. This means once you're in any terminal state, you cannot transition to any other state (rank ≤ current).

**Consequences:**
- Out-of-order detection is O(1) per event.
- The rule is easy to explain: "you can only go forward."
- A `COMPLETED` call cannot be re-opened by a late `FAILED` event.

**Alternatives Considered:**
- Explicit transition table (whitelist) — more expressive but harder to maintain and doesn't naturally handle duplicates.
- `frozenset` of valid next states per state — same problem.

---

## ADR-004: Event Idempotency via Processed Event ID Set

**Status:** Accepted

**Context:**
Provider B sends duplicate events with the same `event_id`. The system must be safe to receive any event any number of times.

**Decision:**
`Call.processed_event_ids: set[str]` stores every `event_id` that has been applied. `apply_transition` checks membership before doing anything else. If the `event_id` is already present, return `False` immediately.

Idempotency check takes priority over the rank check. This prevents the subtle bug where a duplicate `COMPLETED` event arrives after a reset (if the system were to support resets), and the rank check would incorrectly allow it.

**Consequences:**
- O(1) duplicate detection using a Python `set`.
- `processed_event_ids` grows with each call lifecycle. In a PostgreSQL implementation, this would be a separate `call_events` table with a unique constraint on `(call_id, event_id)`.
- The `version` counter accurately reflects only applied transitions, not total events received.

---

## ADR-005: EMA for Answer Rate Estimation

**Status:** Accepted

**Context:**
The predictive dialler needs an estimate of the current answer rate to decide how many calls to start. The estimate must be simple enough to explain in a 5-minute interview.

**Decision:**
Use Exponential Moving Average with `alpha = 0.15`:
```
new_rate = alpha * observed + (1 - alpha) * prev_rate
```
Where `observed = 1.0` if answered, `0.0` if not.

**Consequences:**
- One-formula, no external library, traceable by hand.
- `alpha = 0.15` gives a half-life of approximately 4–5 calls — slow enough to be stable, fast enough to adapt.
- The initial rate (`initial_answer_rate = 0.50`) represents a cold-start assumption.
- The formula is visible in `predictive.py` with a comment explaining each term.

**Alternatives Considered:**
- Bayesian estimation — more principled but requires explaining priors in an interview.
- ARIMA or ML model — violates the assignment's "no overengineering" constraint.
- Simple rolling average — doesn't weight recent observations more heavily.

---

## ADR-006: Lease-Based Crash Recovery

**Status:** Accepted

**Context:**
A worker can crash at any point between agent reservation and call INITIATED. If it does, the agent and borrower are permanently locked without a recovery mechanism.

**Decision:**
Write `lease_until = now + N seconds` on both the agent and the call record at reservation time. A periodic `Reconciler` calls `find_expired_reservations()` and cleans up stuck calls.

Recovery policy:
- `QUEUED / RESERVED` → CANCELLED (never reached provider, safe to undo)
- `INITIATED / RINGING` → FAILED (provider was contacted, we can't undo, so declare failure)
- `CONNECTED / ANSWERED` → **do not kill** (live conversation; reconciler logs a warning)

**Consequences:**
- Crash recovery is fully automatic and runs without coordinator consensus.
- The reconciler is idempotent: two instances racing on the same call both call `apply_transition` with the same `event_id = "reconciler-{call_id}"`. The second one sees the duplicate and skips.
- Lease window must be longer than the worst-case setup time. Default is 30 seconds.

**Alternatives Considered:**
- Dead-letter queue — requires an external message broker.
- Saga / two-phase commit — overengineered for this use case.
- Heartbeat renewal — added complexity; lease-at-write is sufficient for this prototype.

---

## ADR-007: Asynchronous Event Delivery via Daemon Threads

**Status:** Accepted

**Context:**
Real telecom providers deliver call events via webhooks, asynchronously. Provider A and Provider B must simulate this without requiring HTTP servers or message queues.

**Decision:**
Each `initiate_call()` spawns a daemon thread that sleeps for simulated delays then calls `event_callback(ProviderEvent)`. The `delay_scale` parameter collapses all delays to zero for fast synchronous test execution.

`event_callback` is `EventProcessor.process` — the same code path used in production.

**Consequences:**
- Tests run in under 35 seconds for 202 tests (delay_scale=0 for unit tests, 0.05 for integration tests).
- Daemon threads don't block process exit.
- The `EventProcessor.process()` method is thread-safe: it reads the call from the store, applies the transition, and writes back. The store's per-row lock ensures atomicity.

**Alternatives Considered:**
- `threading.Event` with explicit signalling — too complex for test scenarios.
- Synchronous callbacks (no thread) — doesn't test the concurrency properties.
- Mock providers returning pre-built event sequences — misses the async timing complexity.

---

## ADR-008: No Global Lock During I/O

**Status:** Accepted

**Context:**
The allocator holds reservations (per-row locks) while calling the provider. If we held a global lock during the provider call, every other thread would stall.

**Decision:**
The allocator acquires the per-row agent lock only for `atomic_reserve_agent()`, then releases it before calling the provider. The per-agent reservation is persisted to the store (with lease) before the provider is called. This means:
- If the worker crashes after reservation but before provider call: reconciler handles it.
- If the provider is slow: only the specific agent+borrower row is "locked" (actually RESERVED state), not the whole store.

**Consequences:**
- High concurrency: N workers can allocate N different agents simultaneously without blocking each other.
- No deadlock risk: locks are fine-grained and always acquired in the same order (never nested).
- The trade-off: the agent is "occupied" for the lease duration even if the worker crashes.

---

## ADR-009: DialMode as Enum on Campaign

**Status:** Accepted

**Context:**
The original prototype used `dial_mode: str` on Campaign. Adding `DialMode.PROGRESSIVE` / `DialMode.PREDICTIVE` as an enum provides type safety and IDE completion.

**Decision:**
Add `DialMode(str, enum.Enum)` to `campaign.py`. The Campaign model uses `DialMode.PREDICTIVE` as the default. The simulation runner and demo scripts import and use the enum directly.

**Consequences:**
- Type-safe. Passing `"predictiv"` (typo) would fail at construction rather than silently producing wrong behaviour.
- Backwards compatible: because `DialMode(str, enum.Enum)`, the values compare equal to their string representations (`DialMode.PREDICTIVE == "predictive"` is `True`).
