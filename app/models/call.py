"""
app/models/call.py
------------------
Call model for the SmartDialer system.

A Call represents a single outbound dialling attempt from an agent to a
borrower.  It travels through a well-defined state machine and carries the
idempotency machinery needed to handle duplicate or out-of-order provider
events safely.

Problem this solves:
    Telecom providers (especially unreliable ones) can send the same event
    multiple times (duplicates) or in the wrong order (out-of-order).
    Without an explicit state machine and idempotency tracking the system
    could transition a completed call back to "ringing" when a stale event
    arrives.  This model makes both impossible.

Key design decisions:

1.  `version` — a monotonically increasing integer applied to every valid
    state transition.  If an incoming event tries to set a version ≤ current
    version, it is rejected as stale.

2.  `processed_event_ids` — a set of event IDs we have already handled.
    Providers must supply a stable event_id; if we see it again we skip it.

3.  Terminal states (COMPLETED, FAILED, CANCELLED) are a "black hole":
    no event can pull a call out of them.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Set


class CallState(str, enum.Enum):
    """
    Explicit lifecycle states for an outbound call.

    QUEUED      The call has been requested but not yet allocated resources.
    RESERVED    An agent and borrower have been reserved; awaiting provider call.
    INITIATED   The provider has been asked to start the call.
    RINGING     The borrower's phone is ringing.
    ANSWERED    Borrower picked up; waiting for voice path to be established.
    CONNECTED   Full two-way conversation in progress.
    COMPLETED   Call ended normally.
    FAILED      Call ended due to an error (no answer, provider error, etc.).
    CANCELLED   Call was cancelled before the borrower was reached.
    """

    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Terminal states — once here, a call NEVER moves again.
TERMINAL_STATES: Set[CallState] = {
    CallState.COMPLETED,
    CallState.FAILED,
    CallState.CANCELLED,
}

# Ordered list of states for monotonic progression checks.
# A state transition is only valid if the new state's index is strictly
# greater than the current state's index.
STATE_ORDER = [
    CallState.QUEUED,
    CallState.RESERVED,
    CallState.INITIATED,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.CONNECTED,
    CallState.COMPLETED,
    CallState.FAILED,
    CallState.CANCELLED,
]

_STATE_RANK: dict[CallState, int] = {s: i for i, s in enumerate(STATE_ORDER)}


def state_rank(state: CallState) -> int:
    """Return a numeric rank for ordering.  Higher rank = further along."""
    return _STATE_RANK[state]


@dataclass
class Call:
    """
    Core call record stored in the state store.

    Fields
    ------
    id                  Unique call identifier (UUID string).
    agent_id            Agent assigned to this call.
    borrower_id         Borrower being called.
    campaign_id         Campaign this call belongs to.
    provider            Name of the telecom provider handling this call.
    state               Current CallState.
    reservation_id      UUID shared with the agent/borrower reservation.
                        Used by the recovery process to correlate records.
    version             Monotonically-increasing integer.  Incremented on every
                        valid state transition to detect stale events.
    created_at          When the call record was created.
    initiated_at        When the provider was asked to place the call.
    connected_at        When the call became CONNECTED.
    ended_at            When the call reached a terminal state.
    lease_until         Deadline for the current reservation.  If the worker
                        crashes before this expires, recovery reclaims it.
    processed_event_ids Set of event IDs already applied to this call.
                        Prevents duplicate provider events from re-running.
    failure_reason      Human-readable reason for FAILED/CANCELLED.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: Optional[str] = None
    borrower_id: Optional[str] = None
    campaign_id: Optional[str] = None
    provider: Optional[str] = None
    state: CallState = CallState.QUEUED
    reservation_id: Optional[str] = None
    version: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    initiated_at: Optional[datetime] = None
    connected_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    lease_until: Optional[datetime] = None
    processed_event_ids: Set[str] = field(default_factory=set)
    failure_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Idempotency helpers
    # ------------------------------------------------------------------

    def has_processed(self, event_id: str) -> bool:
        """Return True if we have already handled this event_id."""
        return event_id in self.processed_event_ids

    def mark_processed(self, event_id: str) -> None:
        """Record that this event_id has been successfully applied."""
        self.processed_event_ids.add(event_id)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def is_terminal(self) -> bool:
        """Return True if this call is in a terminal state."""
        return self.state in TERMINAL_STATES

    def can_transition_to(self, new_state: CallState) -> bool:
        """
        Return True if transitioning to new_state is valid.

        Rules:
        1. Terminal states cannot transition anywhere.
        2. New state must have a higher rank than the current state.
           This prevents out-of-order events from rolling the call back.

        Note: FAILED and CANCELLED are special terminal states that can
        be reached from many states, so they get a rank above CONNECTED
        but are treated as terminal endpoints.
        """
        if self.is_terminal():
            # Already done — nothing can happen.
            return False

        # Allow jumping to any terminal state from any non-terminal state.
        if new_state in TERMINAL_STATES:
            return True

        return state_rank(new_state) > state_rank(self.state)

    def apply_transition(self, new_state: CallState, event_id: Optional[str] = None) -> bool:
        """
        Attempt to apply a state transition.

        Returns True on success, False if the transition was rejected
        (duplicate event or out-of-order arrival).

        If event_id is provided, also marks it as processed so that a
        duplicate delivery of the same event has no effect.
        """
        # Idempotency check: have we seen this event before?
        if event_id and self.has_processed(event_id):
            return False  # Duplicate — silently drop.

        # Monotonic check: is this a valid progression?
        if not self.can_transition_to(new_state):
            return False  # Out of order or stale — drop.

        # Apply the transition.
        self.state = new_state
        self.version += 1

        if event_id:
            self.mark_processed(event_id)

        # Record timing on key transitions.
        now = datetime.now(timezone.utc)
        if new_state == CallState.INITIATED:
            self.initiated_at = now
        elif new_state == CallState.CONNECTED:
            self.connected_at = now
        elif new_state in TERMINAL_STATES:
            self.ended_at = now

        return True

    def __repr__(self) -> str:
        return (
            f"Call(id={self.id[:8]}…, state={self.state.value}, "
            f"agent={str(self.agent_id)[:8] if self.agent_id else None}…, "
            f"version={self.version})"
        )
