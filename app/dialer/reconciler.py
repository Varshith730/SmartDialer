"""
app/dialer/reconciler.py
------------------------
Worker Crash Recovery / Lease Reconciler.

Problem this solves:
    A worker (thread or process) can crash at any point in the call setup
    pipeline:

        Agent RESERVED ─→ Borrower RESERVED ─→ Call created ─→ (crash)
                                                              ↑ worker dies here

    Without recovery, the agent and borrower stay RESERVED forever.  The
    campaign stalls because those resources appear occupied but nobody is
    using them.

The lease mechanism:
    When the allocator reserves an agent and creates a call, it writes a
    `lease_until` timestamp on both the agent and the call record.  The
    timestamp is set to `now + N seconds`.

    A live worker should finish setup (move the agent to DIALING) well before
    the lease expires.  If the worker crashes, it never transitions the call
    forward, and the lease silently expires.

    The Reconciler is a lightweight periodic job that:
    1. Scans for calls whose `lease_until` has passed.
    2. Inspects the call's current state.
    3. Applies the appropriate recovery action.
    4. Logs every decision for auditability.

Recovery actions by call state:
    RESERVED / INITIATED / RINGING
        The call is clearly stuck — the worker never got the borrower to
        talk to an agent.  Action: mark call CANCELLED (pre-answer) or
        FAILED (post-initiation), release the agent to AVAILABLE, and
        release the borrower back to PENDING.

    ANSWERED / CONNECTED
        This is a live conversation.  The lease might have expired simply
        because the talk time exceeded the lease window.  We do NOT kill a
        live call.  Action: log a warning, do NOT release the agent.
        (A production system would also renew the lease here.)

    QUEUED
        Should not normally have a lease, but if it does and it's expired,
        treat like RESERVED (cancel and release).

    Terminal states (COMPLETED, FAILED, CANCELLED)
        Already resolved — skip.

PostgreSQL equivalent:
    SELECT * FROM calls WHERE lease_until < NOW() AND state NOT IN ('COMPLETED','FAILED','CANCELLED');
    -- Then for each result, UPDATE within a transaction with
    -- WHERE state = <expected_state> to prevent double-processing.

Thread safety:
    The reconciler uses the store's per-row locks via release_agent() and
    release_borrower().  If two reconciler instances race on the same call,
    the second one will find the call already terminal and skip it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.models.call import Call, CallState, TERMINAL_STATES
from app.providers.interface import ProviderEvent
from app.repository.state_store import StateStore

logger = logging.getLogger(__name__)

# States in which a stuck call should be aggressively cleaned up.
# These states mean the call never reached the borrower.
CLEANABLE_STATES = {
    CallState.QUEUED,
    CallState.RESERVED,
    CallState.INITIATED,
    CallState.RINGING,
}

# States that indicate a live conversation — do NOT kill these.
LIVE_CALL_STATES = {
    CallState.ANSWERED,
    CallState.CONNECTED,
}


@dataclass
class ReconciliationResult:
    """
    Summary of a single reconciliation run.

    Fields
    ------
    total_expired       How many calls had expired leases.
    cleaned_up          How many calls were cancelled/failed and resources freed.
    live_calls_skipped  How many ANSWERED/CONNECTED calls were left alone.
    already_terminal    How many calls were already in a terminal state.
    run_at              When the reconciliation ran.
    details             Per-call summary strings (for logging and demo output).
    """

    total_expired: int = 0
    cleaned_up: int = 0
    live_calls_skipped: int = 0
    already_terminal: int = 0
    run_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: list[str] = field(default_factory=list)

    def has_work(self) -> bool:
        """True if at least one expired lease was found."""
        return self.total_expired > 0

    def __repr__(self) -> str:
        return (
            f"ReconciliationResult("
            f"expired={self.total_expired}, "
            f"cleaned={self.cleaned_up}, "
            f"live_skipped={self.live_calls_skipped}, "
            f"terminal_skipped={self.already_terminal})"
        )


class Reconciler:
    """
    Finds and recovers from worker crashes using lease expiry.

    Usage
    -----
        reconciler = Reconciler(store)

        # Run once (e.g. on a timer every 30 seconds):
        result = reconciler.run()
        if result.has_work():
            print(result)

    The reconciler is stateless between runs — it reads fresh state on
    every call to run().  Multiple reconciler instances can safely run
    concurrently because all mutations go through the store's per-row locks.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._total_runs: int = 0
        self._total_cleaned: int = 0

    def run(self) -> ReconciliationResult:
        """
        Scan for expired leases and reconcile each stuck call.

        Returns a ReconciliationResult summarising what was done.
        """
        self._total_runs += 1
        result = ReconciliationResult()

        expired_calls = self._store.find_expired_reservations()
        result.total_expired = len(expired_calls)

        for call in expired_calls:
            action = self._reconcile_call(call)
            result.details.append(
                f"call={call.id[:8]}… state={call.state.value} → {action}"
            )
            if action == "cleaned":
                result.cleaned_up += 1
                self._total_cleaned += 1
            elif action == "live_skipped":
                result.live_calls_skipped += 1
            elif action == "already_terminal":
                result.already_terminal += 1

        if result.has_work():
            logger.warning(
                "[Reconciler] Run #%d: %s",
                self._total_runs,
                result,
            )
        else:
            logger.debug("[Reconciler] Run #%d: nothing to reconcile.", self._total_runs)

        return result

    # ------------------------------------------------------------------
    # Internal: per-call reconciliation
    # ------------------------------------------------------------------

    def _reconcile_call(self, call: Call) -> str:
        """
        Reconcile a single expired-lease call.

        Returns one of: "cleaned", "live_skipped", "already_terminal".
        """
        current_state = call.state

        # Already done — this can happen if two reconcilers run simultaneously:
        # the first cleaned up the call, the second finds it terminal.
        if current_state in TERMINAL_STATES:
            logger.debug(
                "[Reconciler] Call %s already terminal (%s) — skipping.",
                call.id[:8], current_state.value,
            )
            return "already_terminal"

        # Live conversation — do not interrupt.
        if current_state in LIVE_CALL_STATES:
            logger.warning(
                "[Reconciler] Call %s is %s (live call) with expired lease. "
                "NOT terminating — lease window may be too short.",
                call.id[:8], current_state.value,
            )
            return "live_skipped"

        # Stuck in pre-answer state — clean up.
        if current_state in CLEANABLE_STATES:
            return self._cleanup_stuck_call(call)

        # Unexpected state — log and skip safely.
        logger.error(
            "[Reconciler] Call %s in unexpected state %s — skipping.",
            call.id[:8], current_state.value,
        )
        return "already_terminal"

    def _cleanup_stuck_call(self, call: Call) -> str:
        """
        Cancel/fail a stuck call and release its resources.

        Steps:
        1. Apply terminal transition (CANCELLED for pre-initiation,
           FAILED for post-initiation).
        2. Save the call.
        3. Release the agent (back to AVAILABLE).
        4. Release the borrower (back to PENDING for retry).
        """
        # Choose appropriate terminal state.
        # RESERVED / QUEUED → CANCELLED (never reached provider)
        # INITIATED / RINGING → FAILED (provider was contacted but no answer)
        if call.state in {CallState.QUEUED, CallState.RESERVED}:
            terminal = CallState.CANCELLED
            reason = "lease expired — worker crash during setup (pre-provider)"
        else:
            terminal = CallState.FAILED
            reason = "lease expired — worker crash during dialling"

        event_id = f"reconciler-{call.id}"
        applied = call.apply_transition(terminal, event_id=event_id)

        if not applied:
            # Another reconciler or the event processor got here first.
            logger.debug(
                "[Reconciler] Could not apply %s to call %s — already handled.",
                terminal.value, call.id[:8],
            )
            return "already_terminal"

        call.failure_reason = reason
        self._store.save_call(call)

        # Release agent.
        if call.agent_id:
            self._store.release_agent(call.agent_id)
            logger.info(
                "[Reconciler] Released agent %s (call %s %s → %s)",
                call.agent_id[:8], call.id[:8],
                call.state.value,   # already transitioned above
                terminal.value,
            )

        # Release borrower.
        if call.borrower_id:
            self._store.release_borrower(call.borrower_id)
            logger.info(
                "[Reconciler] Released borrower %s (call %s)",
                call.borrower_id[:8], call.id[:8],
            )

        logger.warning(
            "[Reconciler] Cleaned up call %s (%s → %s): %s",
            call.id[:8],
            call.state.value,
            terminal.value,
            reason,
        )
        return "cleaned"

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            "total_runs": self._total_runs,
            "total_cleaned": self._total_cleaned,
        }
