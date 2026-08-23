"""
app/dialer/progressive.py
--------------------------
Progressive Dialer — the simplest, safest dialling strategy.

Rule (the invariant this module enforces):
    At any moment, the number of new outbound calls we can start is at most
    the number of agents that are currently AVAILABLE.

    If 3 agents are available → at most 3 new calls.
    If 0 agents are available → 0 new calls.
    There is never a call without a dedicated agent ready for it.

Why this is the baseline:
    Progressive dialing is the "safety floor".  It is slower than predictive
    dialing (which can have a few calls ringing while agents wrap up), but it
    is completely predictable: a borrower who answers *always* reaches a
    live agent immediately.

    In the architecture, the Safety Controller can fall back to progressive
    behaviour at any time by signalling FALLBACK_PROGRESSIVE.  That fallback
    calls this module.

What the Progressive Dialer does NOT do:
    - It does not decide how many agents are "enough" — it uses all available.
    - It does not pace or predict — it is purely reactive.
    - It does not talk to the provider directly.  It hands AllocationRequests
      to the CallAllocator, which does the actual initiation.

Design note — borrower selection:
    Borrowers are selected via store.list_dialable_borrowers(), which returns
    them sorted by (priority.value ASC, attempt_count ASC).  This means
    high-priority, least-tried borrowers are called first.  The sort order
    is deterministic, so two workers will agree on who to call next (though
    only one will win the atomic reservation).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.dialer.allocator import AllocationRequest, AllocationResult, CallAllocator
from app.models.campaign import Campaign
from app.providers.interface import EventCallback, TelecomProvider
from app.repository.state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass
class ProgressiveCycleResult:
    """
    Summary of one progressive dialler cycle.

    A "cycle" is a single call to run_cycle() — typically triggered on a
    short timer (e.g. every 1-2 seconds) or whenever an agent becomes free.
    """

    available_agents_at_start: int   # How many agents were free when we started.
    calls_attempted: int             # How many allocation requests we made.
    calls_succeeded: int             # How many were actually initiated.
    calls_failed: int                # How many failed (provider refused, race, etc.).
    results: list[AllocationResult]  # Full details for each attempt.

    def __repr__(self) -> str:
        return (
            f"ProgressiveCycleResult("
            f"available={self.available_agents_at_start}, "
            f"attempted={self.calls_attempted}, "
            f"succeeded={self.calls_succeeded}, "
            f"failed={self.calls_failed})"
        )


class ProgressiveDialer:
    """
    Drives progressive (1-agent-to-1-call) outbound dialling.

    Usage
    -----
        dialer = ProgressiveDialer(store, allocator)
        result = dialer.run_cycle(campaign, provider)

    The caller (e.g. a scheduler loop) should call run_cycle() periodically.
    Each call makes at most `available_agents` new allocation attempts.

    Parameters
    ----------
    store       The shared state store.
    allocator   The CallAllocator that handles agent/borrower reservation
                and provider hand-off.
    max_per_cycle  Optional cap on calls per cycle.  Useful for throttling
                   in tests or when winding down a campaign.  Defaults to
                   unlimited (None = use all available agents).
    """

    def __init__(
        self,
        store: StateStore,
        allocator: CallAllocator,
        max_per_cycle: Optional[int] = None,
    ) -> None:
        self._store = store
        self._allocator = allocator
        self._max_per_cycle = max_per_cycle

    def run_cycle(
        self,
        campaign: Campaign,
        provider: TelecomProvider,
        event_callback: Optional[EventCallback] = None,
    ) -> ProgressiveCycleResult:
        """
        Run one progressive dialling cycle for the given campaign.

        Algorithm
        ---------
        1. Fetch all currently AVAILABLE agents.
        2. Determine the slot count:
               slots = min(len(available_agents), max_per_cycle or ∞)
           This is the progressive invariant: at most 1 call per available agent.
        3. For each slot:
           a. Pop the next available agent from the list.
           b. Pick the highest-priority dialable borrower for this campaign.
           c. Build an AllocationRequest and call the allocator.
           d. If the allocator fails (race, provider rejected), continue to
              the next slot.  We do NOT retry the same agent — the next cycle
              will pick it up if it is still available.
        4. Return a summary.

        Thread safety:
            Two dialler threads could run simultaneously.  Both will see the
            same available-agent list (a snapshot), but atomic_reserve_agent
            in the allocator ensures only one wins each agent.  The other
            will get AllocationResult(success=False) and skip it.  No double-
            allocation can occur.
        """
        if not campaign.is_active():
            logger.debug(
                "Progressive cycle skipped: campaign %s is not ACTIVE (status=%s)",
                campaign.id[:8],
                campaign.status.value,
            )
            return ProgressiveCycleResult(
                available_agents_at_start=0,
                calls_attempted=0,
                calls_succeeded=0,
                calls_failed=0,
                results=[],
            )

        # ------------------------------------------------------------------
        # Step 1: Snapshot of available agents
        # ------------------------------------------------------------------
        available_agents = self._store.list_available_agents()
        n_available = len(available_agents)

        # ------------------------------------------------------------------
        # Step 2: Compute slot count (the progressive invariant)
        # ------------------------------------------------------------------
        slots = n_available
        if self._max_per_cycle is not None:
            slots = min(slots, self._max_per_cycle)

        logger.debug(
            "Progressive cycle: campaign=%s available_agents=%d slots=%d",
            campaign.id[:8],
            n_available,
            slots,
        )

        if slots == 0:
            return ProgressiveCycleResult(
                available_agents_at_start=n_available,
                calls_attempted=0,
                calls_succeeded=0,
                calls_failed=0,
                results=[],
            )

        # ------------------------------------------------------------------
        # Step 3: For each slot, pick agent + borrower and call allocator
        # ------------------------------------------------------------------
        results: list[AllocationResult] = []
        succeeded = 0
        failed = 0

        # We take a snapshot of dialable borrowers once per cycle.
        # The atomic_reserve_borrower call inside the allocator prevents
        # two workers from dialling the same person even if they both see
        # the same snapshot.
        dialable_borrowers = self._store.list_dialable_borrowers(campaign.id)

        # Iterate over agents and borrowers in parallel using a pointer.
        # Each agent gets paired with one borrower.
        borrower_index = 0

        for agent in available_agents[:slots]:
            if borrower_index >= len(dialable_borrowers):
                # No more borrowers to dial in this campaign cycle.
                logger.info(
                    "Progressive cycle: no more dialable borrowers for campaign %s",
                    campaign.id[:8],
                )
                break

            borrower = dialable_borrowers[borrower_index]
            borrower_index += 1

            request = AllocationRequest(
                agent_id=agent.id,
                borrower_id=borrower.id,
                campaign_id=campaign.id,
                provider_name=provider.name,
            )

            result = self._allocator.allocate(request, provider, event_callback)
            results.append(result)

            if result.success:
                succeeded += 1
                logger.debug(
                    "Progressive: call initiated agent=%s borrower=%s",
                    agent.id[:8],
                    borrower.id[:8],
                )
            else:
                failed += 1
                logger.debug(
                    "Progressive: allocation failed agent=%s borrower=%s reason=%r",
                    agent.id[:8],
                    borrower.id[:8],
                    result.failure_reason,
                )

        return ProgressiveCycleResult(
            available_agents_at_start=n_available,
            calls_attempted=len(results),
            calls_succeeded=succeeded,
            calls_failed=failed,
            results=results,
        )
