"""
app/repository/state_store.py
------------------------------
In-memory state store for the SmartDialer prototype.

This module is the ONLY place where shared mutable state lives.
Everything else reads or writes through this interface.

Problem this solves:
    In a concurrent system multiple worker threads may try to reserve the
    same agent simultaneously.  Without careful locking, both workers could
    read "agent is AVAILABLE", both decide to reserve it, and both succeed —
    resulting in the same agent being double-allocated.

Concurrency strategy:
    We use a single threading.Lock per agent (and per borrower) for the
    critical sections.  The atomic_reserve_agent method is the key routine:
    it checks state and writes the new state while holding the lock so that
    only one thread can ever succeed.

How this maps to PostgreSQL in production:
    The in-memory lock corresponds to a PostgreSQL row-level lock combined
    with an optimistic-concurrency WHERE clause:

        UPDATE agents
        SET    state          = 'RESERVED',
               reservation_id = :rid,
               lease_until    = :deadline
        WHERE  id             = :agent_id
        AND    state          = 'AVAILABLE';

    If exactly 1 row is affected the caller won wins; if 0 rows are
    affected another worker got there first.  Postgres serializes concurrent
    UPDATE statements on the same row, so no additional application lock is
    needed in the database path.

Design intent:
    The interfaces (get_agent, save_agent, etc.) deliberately mirror what a
    PostgreSQL repository would expose.  Swapping this module for a real
    database layer requires only replacing the bodies of these methods.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from app.models.agent import Agent, AgentState
from app.models.borrower import Borrower, BorrowerStatus
from app.models.call import Call, CallState, TERMINAL_STATES
from app.models.campaign import Campaign


class StateStore:
    """
    Thread-safe in-memory repository for all domain objects.

    Internal layout
    ---------------
    _agents    : dict[agent_id, Agent]
    _borrowers : dict[borrower_id, Borrower]
    _calls     : dict[call_id, Call]
    _campaigns : dict[campaign_id, Campaign]

    Each entity dict has its own RLock so that, for example, a borrower
    lookup does not block an unrelated agent reservation.

    The atomic_reserve_agent method uses the agent's own lock.  Because we
    always acquire only one lock at a time (never two simultaneously) we
    avoid deadlocks entirely.
    """

    def __init__(self) -> None:
        # Storage dicts
        self._agents: Dict[str, Agent] = {}
        self._borrowers: Dict[str, Borrower] = {}
        self._calls: Dict[str, Call] = {}
        self._campaigns: Dict[str, Campaign] = {}

        # Per-entity type locks for bulk queries
        self._agents_lock = threading.Lock()
        self._borrowers_lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self._campaigns_lock = threading.Lock()

        # Per-agent fine-grained locks for atomic reservation.
        # Key: agent_id → Lock
        # These are created lazily when an agent is first saved.
        self._agent_row_locks: Dict[str, threading.Lock] = {}

        # Per-borrower fine-grained locks for atomic reservation.
        self._borrower_row_locks: Dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    def save_agent(self, agent: Agent) -> None:
        """Persist (insert or update) an agent record."""
        with self._agents_lock:
            self._agents[agent.id] = agent
            # Ensure a row lock exists for new agents.
            if agent.id not in self._agent_row_locks:
                self._agent_row_locks[agent.id] = threading.Lock()

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        """Fetch a single agent by ID.  Returns None if not found."""
        with self._agents_lock:
            return self._agents.get(agent_id)

    def list_agents(self) -> List[Agent]:
        """Return a snapshot list of all agents."""
        with self._agents_lock:
            return list(self._agents.values())

    def list_available_agents(self) -> List[Agent]:
        """Return all agents currently in AVAILABLE state."""
        with self._agents_lock:
            return [a for a in self._agents.values() if a.state == AgentState.AVAILABLE]

    def count_agents_by_state(self) -> Dict[str, int]:
        """Return counts of agents grouped by state string."""
        with self._agents_lock:
            counts: Dict[str, int] = {}
            for agent in self._agents.values():
                key = agent.state.value
                counts[key] = counts.get(key, 0) + 1
            return counts

    # ------------------------------------------------------------------
    # Atomic agent reservation  ← CRITICAL SECTION
    # ------------------------------------------------------------------

    def atomic_reserve_agent(
        self,
        agent_id: str,
        reservation_id: str,
        lease_seconds: float = 30.0,
    ) -> bool:
        """
        Atomically attempt to move an agent from AVAILABLE → RESERVED.

        This is the most critical operation in the system.

        How it works:
          1. Acquire the per-agent row lock (only one thread can hold this).
          2. Re-read the agent's state under the lock.
          3. If state is AVAILABLE, set it to RESERVED and return True.
          4. If state is anything else, return False.
          5. Release the lock (via context manager).

        Why this is safe:
          Two workers that both saw AVAILABLE will both try to acquire
          the same per-agent lock.  Only one succeeds at step 1; the
          other blocks.  When the winner releases the lock, the loser
          re-reads the state (step 2) and finds RESERVED — so it returns
          False.  The agent is allocated exactly once.

        Parameters
        ----------
        agent_id        : The agent to reserve.
        reservation_id  : A unique ID for this reservation attempt.
                          Shared with the corresponding Call and Borrower
                          records so the recovery process can correlate them.
        lease_seconds   : How long the reservation is valid.  If the worker
                          crashes before transitioning the agent to DIALING,
                          the lease expiry allows recovery to reclaim it.

        Returns True on success, False if the agent was not available.
        """
        row_lock = self._agent_row_locks.get(agent_id)
        if row_lock is None:
            # Agent doesn't exist.
            return False

        with row_lock:
            agent = self._agents.get(agent_id)
            if agent is None or not agent.is_reservable():
                # Already taken or doesn't exist.
                return False

            # ---- Winner section: we hold the lock, state is AVAILABLE ----
            agent.state = AgentState.RESERVED
            agent.reservation_id = reservation_id
            agent.reserved_at = datetime.now(timezone.utc)
            agent.lease_until = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
            return True

    def release_agent(self, agent_id: str) -> None:
        """
        Release a reserved/dialing agent back to AVAILABLE.

        Called by the recovery process or when a call setup fails.
        """
        row_lock = self._agent_row_locks.get(agent_id)
        if row_lock is None:
            return

        with row_lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                return
            agent.state = AgentState.AVAILABLE
            agent.reservation_id = None
            agent.borrower_id = None
            agent.call_id = None
            agent.lease_until = None
            agent.reserved_at = None
            agent.available_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Borrower CRUD
    # ------------------------------------------------------------------

    def save_borrower(self, borrower: Borrower) -> None:
        """Persist (insert or update) a borrower record."""
        with self._borrowers_lock:
            self._borrowers[borrower.id] = borrower
            if borrower.id not in self._borrower_row_locks:
                self._borrower_row_locks[borrower.id] = threading.Lock()

    def get_borrower(self, borrower_id: str) -> Optional[Borrower]:
        """Fetch a single borrower by ID."""
        with self._borrowers_lock:
            return self._borrowers.get(borrower_id)

    def list_borrowers(self, campaign_id: Optional[str] = None) -> List[Borrower]:
        """
        Return all borrowers, optionally filtered by campaign.

        Sorted deterministically: ascending priority, then ascending
        attempt_count so low-attempt borrowers are tried first within
        the same priority tier.
        """
        with self._borrowers_lock:
            borrowers = list(self._borrowers.values())

        if campaign_id:
            borrowers = [b for b in borrowers if b.campaign_id == campaign_id]

        # Deterministic ordering: priority first, then fewest attempts.
        borrowers.sort(key=lambda b: (b.priority.value, b.attempt_count))
        return borrowers

    def list_dialable_borrowers(self, campaign_id: Optional[str] = None) -> List[Borrower]:
        """Return only borrowers that can be called right now."""
        return [b for b in self.list_borrowers(campaign_id) if b.is_dialable()]

    def atomic_reserve_borrower(
        self,
        borrower_id: str,
        reservation_id: str,
    ) -> bool:
        """
        Atomically attempt to move a borrower from PENDING → RESERVED.

        Same locking pattern as atomic_reserve_agent.
        Prevents two workers from dialling the same person simultaneously.
        """
        row_lock = self._borrower_row_locks.get(borrower_id)
        if row_lock is None:
            return False

        with row_lock:
            borrower = self._borrowers.get(borrower_id)
            if borrower is None or not borrower.is_dialable():
                return False

            borrower.status = BorrowerStatus.RESERVED
            borrower.reserved_by = reservation_id
            borrower.attempt_count += 1
            borrower.last_attempt = datetime.now(timezone.utc)
            return True

    def release_borrower(self, borrower_id: str) -> None:
        """Release a reserved borrower back to PENDING (for retry)."""
        row_lock = self._borrower_row_locks.get(borrower_id)
        if row_lock is None:
            return

        with row_lock:
            borrower = self._borrowers.get(borrower_id)
            if borrower is None:
                return
            borrower.status = BorrowerStatus.PENDING
            borrower.reserved_by = None

    # ------------------------------------------------------------------
    # Call CRUD
    # ------------------------------------------------------------------

    def save_call(self, call: Call) -> None:
        """Persist (insert or update) a call record."""
        with self._calls_lock:
            self._calls[call.id] = call

    def get_call(self, call_id: str) -> Optional[Call]:
        """Fetch a single call by ID."""
        with self._calls_lock:
            return self._calls.get(call_id)

    def list_calls(self, campaign_id: Optional[str] = None) -> List[Call]:
        """Return all calls, optionally filtered by campaign."""
        with self._calls_lock:
            calls = list(self._calls.values())
        if campaign_id:
            calls = [c for c in calls if c.campaign_id == campaign_id]
        return calls

    def count_calls_by_state(self, campaign_id: Optional[str] = None) -> Dict[str, int]:
        """Return call counts grouped by state."""
        counts: Dict[str, int] = {}
        for call in self.list_calls(campaign_id):
            key = call.state.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def list_active_calls(self, campaign_id: Optional[str] = None) -> List[Call]:
        """Return calls that are not in a terminal state."""
        return [
            c for c in self.list_calls(campaign_id)
            if c.state not in TERMINAL_STATES
        ]

    def find_expired_reservations(self) -> List[Call]:
        """
        Return calls whose lease has expired and are not yet terminal.

        Used by the crash-recovery / reconciliation process to find
        calls that a crashed worker was handling.
        """
        now = datetime.now(timezone.utc)
        result = []
        for call in self.list_calls():
            if (
                call.lease_until is not None
                and call.lease_until < now
                and not call.is_terminal()
            ):
                result.append(call)
        return result

    # ------------------------------------------------------------------
    # Campaign CRUD
    # ------------------------------------------------------------------

    def save_campaign(self, campaign: Campaign) -> None:
        """Persist a campaign record."""
        with self._campaigns_lock:
            self._campaigns[campaign.id] = campaign

    def get_campaign(self, campaign_id: str) -> Optional[Campaign]:
        """Fetch a single campaign by ID."""
        with self._campaigns_lock:
            return self._campaigns.get(campaign_id)

    def list_campaigns(self) -> List[Campaign]:
        """Return all campaigns."""
        with self._campaigns_lock:
            return list(self._campaigns.values())

    # ------------------------------------------------------------------
    # Snapshot / diagnostics
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """
        Return a diagnostic snapshot of all entity counts.
        Useful for logging and the simulation reporter.
        """
        agent_counts = self.count_agents_by_state()
        call_counts = self.count_calls_by_state()
        return {
            "agents": {
                "total": len(self._agents),
                "by_state": agent_counts,
            },
            "borrowers": {
                "total": len(self._borrowers),
                "dialable": len(self.list_dialable_borrowers()),
            },
            "calls": {
                "total": len(self._calls),
                "by_state": call_counts,
            },
            "campaigns": {
                "total": len(self._campaigns),
            },
        }
