"""
app/models/campaign.py
----------------------
Campaign model for the SmartDialer system.

A Campaign is the top-level container that groups borrowers together for a
dialling run.  It holds the configuration that the pacing engine and safety
controller read to make their decisions (e.g. max calls per agent, provider
choice, hard call limits).

Problem this solves:
    Different debt portfolios have different urgency levels, different
    answer-rate profiles, and different regulatory limits.  The Campaign
    record keeps these settings in one place so the rest of the system
    can be generic.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class CampaignStatus(str, enum.Enum):
    """Lifecycle status of a campaign."""

    DRAFT = "DRAFT"          # Not yet started.
    ACTIVE = "ACTIVE"        # Currently dialling.
    PAUSED = "PAUSED"        # Temporarily stopped.
    COMPLETED = "COMPLETED"  # All borrowers processed.
    CANCELLED = "CANCELLED"  # Aborted before completion.


class DialMode(str, enum.Enum):
    """Dialling strategy for a campaign."""

    PROGRESSIVE = "progressive"   # 1 call per available agent (safe, conservative).
    PREDICTIVE  = "predictive"    # EMA-based over-dialling (efficient, adaptive).


@dataclass
class Campaign:
    """
    Core campaign record.

    Fields
    ------
    id                  Unique campaign identifier.
    name                Human-readable label.
    status              Current CampaignStatus.
    provider_name       Which telecom provider to use (e.g. "provider_a").
    max_calls_per_agent Hard limit: never put more than this many concurrent
                        calls per available agent (overrides pacing engine).
    max_concurrent_calls Absolute hard cap on simultaneous outbound calls
                        regardless of agent count.  Safety controller enforces.
    dial_mode           DialMode enum (PROGRESSIVE or PREDICTIVE).
    created_at          When the campaign was created.
    started_at          When it was last activated.
    ended_at            When it completed or was cancelled.
    borrower_ids        Ordered list of borrower IDs belonging to this campaign.
                        Maintained in the repository; listed here as metadata.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "Campaign"
    status: CampaignStatus = CampaignStatus.DRAFT
    provider_name: str = "provider_a"
    max_calls_per_agent: float = 3.0     # predictive pacing ceiling
    max_concurrent_calls: int = 100      # hard safety ceiling
    dial_mode: DialMode = DialMode.PREDICTIVE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    def is_active(self) -> bool:
        """Return True if the campaign is currently running."""
        return self.status == CampaignStatus.ACTIVE

    def __repr__(self) -> str:
        return (
            f"Campaign(id={self.id[:8]}…, name={self.name!r}, "
            f"status={self.status.value}, mode={self.dial_mode})"
        )
