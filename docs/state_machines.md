# State Machines

## 1. Call State Machine

### States

| State | Meaning |
|-------|---------|
| `QUEUED` | Call record created, waiting for agent assignment. |
| `RESERVED` | Agent and borrower reserved; call pending provider handoff. |
| `INITIATED` | Provider accepted the call; phone is connecting. |
| `RINGING` | Borrower's phone is ringing. |
| `ANSWERED` | Borrower picked up; voice path being established. |
| `CONNECTED` | Full duplex voice; agent is talking to borrower. |
| `COMPLETED` | Call ended normally (agent hung up). Terminal. |
| `FAILED` | Call did not connect (no answer, busy, network error). Terminal. |
| `CANCELLED` | Call was aborted before reaching the provider (lease expiry, campaign pause). Terminal. |

### Transitions

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                                                         │
QUEUED ──────────► RESERVED ──────────► INITIATED ──────────► RINGING        │
                                                                  │           │
                                                                  │ answered  │
                                                                  ▼           │
                                                              ANSWERED        │
                                                                  │           │
                                                                  │ connected │
                                                                  ▼           │
                                                              CONNECTED       │
                                                                  │           │
                                                                  │ completed │
                                                                  ▼           │
                                                              COMPLETED ──────┘ (terminal)
                                                                              │
                    Any non-terminal state ──────────────────► FAILED ────────┘ (terminal)
                    Any non-terminal state ──────────────────► CANCELLED ─────┘ (terminal)
```

### Ordering Invariant

Each state has a monotonically increasing **rank**:

```python
STATE_ORDER = [
    QUEUED,     # rank 0
    RESERVED,   # rank 1
    INITIATED,  # rank 2
    RINGING,    # rank 3
    ANSWERED,   # rank 4
    CONNECTED,  # rank 5
    COMPLETED,  # rank 6
    FAILED,     # rank 6
    CANCELLED,  # rank 6
]
```

`apply_transition(new_state, event_id)` rejects any transition where `rank(new_state) ≤ rank(current_state)`, **unless** the `event_id` is already in `processed_event_ids` (duplicate detection takes priority).

### Terminal State Rule

`COMPLETED`, `FAILED`, and `CANCELLED` are **black holes**. No transition out of a terminal state is ever accepted, including transitions to other terminal states.

```python
# From COMPLETED:
call.apply_transition(FAILED, "e1")    # → False (COMPLETED rank == FAILED rank)
call.apply_transition(ANSWERED, "e2")  # → False (ANSWERED rank < COMPLETED rank)
call.apply_transition(COMPLETED, "e3") # → False (same state, not strictly greater)
```

### Idempotency Rule

Every call to `apply_transition` must supply a unique `event_id`. If the same `event_id` appears again, the transition is dropped **regardless** of whether it would otherwise be valid.

```python
call.apply_transition(RINGING, "event-001")   # → True, version=1
call.apply_transition(RINGING, "event-001")   # → False (duplicate)
call.apply_transition(ANSWERED, "event-001")  # → False (duplicate, even though ANSWERED is forward)
call.apply_transition(ANSWERED, "event-002")  # → True, version=2
```

This handles Provider B's behaviour of re-sending the same event multiple times.

### Timestamps

Certain transitions record timestamps on the Call:

| Transition | Field set |
|------------|-----------|
| `→ RESERVED` | `reserved_at` |
| `→ INITIATED` | `initiated_at` |
| `→ RINGING` | `ringing_at` |
| `→ ANSWERED` | `answered_at` |
| `→ CONNECTED` | `connected_at` |
| `→ COMPLETED/FAILED/CANCELLED` | `ended_at` |

The EventProcessor uses `connected_at` and `ended_at` to compute talk duration for the pacing engine's EMA.

---

## 2. Agent State Machine

### States

| State | Meaning |
|-------|---------|
| `OFFLINE` | Agent is not logged in; cannot receive calls. |
| `AVAILABLE` | Agent is ready to take a call. |
| `RESERVED` | Agent is being assigned to a borrower (reservation in progress). |
| `DIALING` | Agent is assigned; the provider is connecting the call. |
| `CONNECTED` | Agent is in an active conversation. |
| `WRAP_UP` | Call ended; agent is completing call notes. |
| `PAUSED` | Agent is on a break. |

### Transitions

```
OFFLINE ──────────────────────────────────────────► AVAILABLE
                                                        │
PAUSED ─────────────────────────────────────────────────┘
                                                        │
                                                    (allocator)
                                                        ▼
                                                    RESERVED
                                                        │
                                                    (allocator)
                                                        ▼
                                                    DIALING
                                                        │
                                             (provider: RINGING/ANSWERED)
                                                        ▼
                                                    CONNECTED
                                                        │
                                              (call COMPLETED)
                                                        ▼
                                                    WRAP_UP
                                                        │
                                              (wrap-up complete)
                                                        ▼
                                                    AVAILABLE ◄─────────────────┐
                                                        │                       │
                                             (call FAILED/CANCELLED)            │
                                                        └───────────────────────┘
```

### Reservability

`agent.is_reservable()` returns `True` when `agent.state in (AVAILABLE, PAUSED)`.

The allocator only calls `atomic_reserve_agent()` on agents that the `list_available_agents()` snapshot returns — but it must re-check reservability inside the per-row lock, because another thread may have reserved the agent between the snapshot and the lock acquisition.

### Crash Recovery

When the reconciler detects an expired lease on a call:
1. It releases the agent via `store.release_agent(agent_id)`.
2. `release_agent()` sets `agent.state = AVAILABLE` and clears `reservation_id`, `lease_until`, `call_id`, `borrower_id`.
3. The agent is immediately available for the next dialling cycle.

---

## 3. Borrower State Machine

### States

| State | Meaning |
|-------|---------|
| `PENDING` | Not yet called; eligible for dialling. |
| `RESERVED` | Being called in this cycle (atomic reservation held). |
| `COMPLETED` | An answered call occurred; do not retry in this campaign run. |
| `EXCLUDED` | Manually excluded (DNC, dispute, etc.); never call. |

### Transitions

```
PENDING ─────────────────────────────────────────► RESERVED
                                                       │
                                           (call COMPLETED)
                                                       ▼
                                                   COMPLETED
                                                       │
                                           (call FAILED/CANCELLED)
                                                       ▼
                                                   PENDING  (retry eligible)
                                                       │
                                     EXCLUDED (manual override, no recovery)
                                                       ▼
                                                   EXCLUDED
```

### Dialability

`borrower.is_dialable()` returns `True` when `borrower.status == PENDING`.

`list_dialable_borrowers(campaign_id)` returns PENDING borrowers for a campaign, sorted by:
1. Priority ascending (`HIGH=1 < MEDIUM=2 < LOW=3`).
2. Created date ascending (FIFO within priority tier).

---

## 4. Circuit Breaker State Machine

```
         ┌──────────────────── cooldown elapsed ─────────────────────┐
         │                                                            │
      CLOSED ─── failures ≥ threshold ──► OPEN ────────────────► HALF_OPEN
         │                                                            │
         │◄──────── probe success ────────────────────────────────────┘
         │                                                            │
         │                               OPEN ◄──── probe failure ───┘
```

**CLOSED**: All calls permitted. Failure counter increments on each failed call. Resets to 0 on any success.

**OPEN**: No calls permitted. Entered when `failure_counter ≥ failure_threshold` (default: 5). After `cooldown_seconds`, transitions to HALF_OPEN.

**HALF_OPEN**: Exactly one probe call is permitted. This is enforced atomically: `is_call_permitted()` checks the state and claims the probe slot in the same lock acquisition. Concurrent threads see the circuit as OPEN and are rejected. If the probe succeeds, transitions to CLOSED. If it fails, returns to OPEN (with a new cooldown).

The Safety Controller reads circuit breaker state before every dialling cycle:
- OPEN → REJECT (no calls).
- HALF_OPEN → FALLBACK_PROGRESSIVE (safe single-call mode, uses the probe slot).
- CLOSED → normal evaluation.
