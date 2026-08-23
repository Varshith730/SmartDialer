"""
app/models/borrower.py
----------------------
Borrower model for the SmartDialer system.

A Borrower is the person who owes a debt and will be called by the dialer.
In a collections context, borrowers have different priorities (how urgently
they need to be contacted) and the system needs to track how many times
we've already tried to reach them.

Problem this solves:
    We need a stable, deterministic record per debtor so the pacing engine
    can decide who to call next, and so we avoid double-dialling the same
    person at the same time.

Selection strategy (prototype):
    Simple priority-based ordering.  No ML required.
    High-priority borrowers are tried first.
    Within the same priority tier, we prefer borrowers with fewer attempts.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class BorrowerStatus(str, enum.Enum):
    """
    Lifecycle status of a borrower record.

    PENDING      Not yet called in this campaign run.
    RESERVED     Currently being dialled by an active worker.
    IN_PROGRESS  A call is ringing or connected.
    COMPLETED    Successfully reached (promised to pay, left message, etc.).
    FAILED       All dialling attempts exhausted without contact.
    DO_NOT_CALL  Regulatory or borrower opt-out; must never be dialled.
    """

    PENDING = "PENDING"
    RESERVED = "RESERVED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DO_NOT_CALL = "DO_NOT_CALL"


class BorrowerPriority(int, enum.Enum):
    """
    Numeric priority; lower numbers = higher urgency.
    We use integers so we can sort directly.
    """

    HIGH = 1
    MEDIUM = 2
    LOW = 3


@dataclass
class Borrower:
    """
    Core borrower record stored in the state store.

    Fields
    ------
    id              Unique borrower identifier (UUID string).
    name            Human-readable name for demo output.
    phone_number    The number to dial.
    priority        BorrowerPriority — drives selection ordering.
    attempt_count   How many times we have tried to reach this borrower.
    last_attempt    Timestamp of the most recent call attempt.
    status          Current BorrowerStatus.
    reserved_by     reservation_id of the worker currently handling this
                    borrower (prevents two workers dialling the same person).
    campaign_id     Which campaign owns this borrower record.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Borrower"
    phone_number: str = "+10000000000"
    priority: BorrowerPriority = BorrowerPriority.MEDIUM
    attempt_count: int = 0
    last_attempt: Optional[datetime] = None
    status: BorrowerStatus = BorrowerStatus.PENDING
    reserved_by: Optional[str] = None      # reservation_id, not worker id
    campaign_id: Optional[str] = None

    def is_dialable(self) -> bool:
        """
        Return True only if this borrower can be included in a new call.

        A borrower is dialable only when:
        - They are not on the Do-Not-Call list.
        - They are not already being handled by another worker (RESERVED).
        - They have not already been successfully completed.
        """
        return self.status == BorrowerStatus.PENDING

    def __repr__(self) -> str:
        return (
            f"Borrower(id={self.id[:8]}…, name={self.name!r}, "
            f"priority={self.priority.name}, attempts={self.attempt_count}, "
            f"status={self.status.value})"
        )
