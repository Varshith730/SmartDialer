"""
app/models/agent.py
-------------------
Agent model for the SmartDialer system.

An Agent represents a human call-centre worker who can be allocated to a call.
The agent transitions through explicit states as the dialer reserves, dials,
connects and wraps up each call.

Problem this solves:
    We need to track which agents are free, which are busy, and which are mid-
    setup so that the dialer never double-allocates the same agent.

State machine (simplified):
    OFFLINE → AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP → AVAILABLE
                                         ↓ (failure)
                                      AVAILABLE
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class AgentState(str, enum.Enum):
    """
    All possible lifecycle states for an agent.

    Using str-enum means the values serialize to plain strings, which makes
    debugging and logging straightforward.
    """

    OFFLINE = "OFFLINE"          # Agent is not logged in.
    AVAILABLE = "AVAILABLE"      # Logged in, waiting for a call.
    RESERVED = "RESERVED"        # Temporarily held by one worker during setup.
    DIALING = "DIALING"          # Outbound call is ringing.
    CONNECTED = "CONNECTED"      # Agent is speaking with a borrower.
    WRAP_UP = "WRAP_UP"          # Call finished; agent completing notes.
    PAUSED = "PAUSED"            # Agent requested a short break.


# The set of states from which an agent can accept a new reservation.
# Used by the repository's atomic reserve operation.
RESERVABLE_STATES = {AgentState.AVAILABLE}

# Terminal states after which no reservation should be attempted.
ACTIVE_STATES = {
    AgentState.RESERVED,
    AgentState.DIALING,
    AgentState.CONNECTED,
    AgentState.WRAP_UP,
}


@dataclass
class Agent:
    """
    Core agent record stored in the state store.

    Fields
    ------
    id              Unique agent identifier (UUID string).
    name            Human-readable label for logs/demo output.
    state           Current lifecycle state.
    reservation_id  The UUID of the active reservation, if any.
                    Set when state moves to RESERVED; cleared on release.
    borrower_id     The borrower this agent is currently handling.
    call_id         The call record this agent is currently associated with.
    lease_until     Absolute datetime at which the current reservation expires.
                    The recovery process uses this to detect crashed workers.
    reserved_at     When the agent was last moved into RESERVED state.
    available_at    When the agent last became AVAILABLE (useful for reporting).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Agent"
    state: AgentState = AgentState.AVAILABLE
    reservation_id: Optional[str] = None
    borrower_id: Optional[str] = None
    call_id: Optional[str] = None
    lease_until: Optional[datetime] = None
    reserved_at: Optional[datetime] = None
    available_at: Optional[datetime] = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_reservable(self) -> bool:
        """Return True only if this agent can be reserved right now."""
        return self.state in RESERVABLE_STATES

    def is_active(self) -> bool:
        """Return True if the agent is mid-call (not free and not offline)."""
        return self.state in ACTIVE_STATES

    def __repr__(self) -> str:
        return (
            f"Agent(id={self.id[:8]}…, name={self.name!r}, "
            f"state={self.state.value}, reservation={self.reservation_id})"
        )
