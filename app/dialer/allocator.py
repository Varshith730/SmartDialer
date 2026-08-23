"""
app/dialer/allocator.py
-----------------------
Call Allocator — the component that EXECUTES an approved call.

Problem this solves:
    Once the Safety Controller has said "start N calls", something must
    do the actual work: reserve an agent, reserve a borrower, create the
    call record, move the agent to DIALING, and hand off to the provider.
    If any step fails the allocator must clean up so that no resource is
    left in a half-reserved state.

What the allocator does NOT do:
    - It does not decide HOW MANY calls to start (that is the pacing engine).
    - It does not decide WHICH campaign to run (that is the campaign manager).
    - It does not bypass the safety controller (it is always called AFTER
      the safety controller has approved a count).

Failure handling:
    Agent reservation fails   → return failure, nothing to clean up.
    Borrower reservation fails → release agent, return failure.
    Provider rejects call      → mark call FAILED, release agent,
                                 release borrower, return failure.

This means every failure path leaves the system in a consistent state:
agents and borrowers are always back to AVAILABLE/PENDING if we did not
successfully initiate the call with the provider.

Thread safety:
    The allocator itself holds no shared state.  All mutation goes through
    the StateStore which uses per-row locks.  Multiple allocator instances
    (or one instance called from multiple threads) are safe.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models.agent import AgentState
from app.models.borrower import BorrowerStatus
from app.models.call import Call, CallState
from app.providers.interface import TelecomProvider, EventCallback
from app.repository.state_store import StateStore

logger = logging.getLogger(__name__)

# How long an agent/borrower reservation is valid before the recovery process
# can reclaim it.  If a worker crashes after reserving but before dialling,
# the lease expiry prevents the resource being locked forever.
DEFAULT_LEASE_SECONDS = 30.0


@dataclass
class AllocationRequest:
    """
    All the information needed to allocate one outbound call.

    agent_id and borrower_id are the candidates chosen by the caller
    (progressive dialer or predictive pacing engine via the allocator).
    """

    agent_id: str
    borrower_id: str
    campaign_id: str
    provider_name: str
    lease_seconds: float = DEFAULT_LEASE_SECONDS


@dataclass
class AllocationResult:
    """
    The outcome of a single allocation attempt.

    success        True if the call was successfully initiated with the provider.
    call           The Call record if success is True.
    agent_id       Echoed back for the caller's convenience.
    borrower_id    Echoed back for the caller's convenience.
    failure_reason Short description of why the allocation failed.
    """

    success: bool
    call: Optional[Call] = None
    agent_id: str = ""
    borrower_id: str = ""
    failure_reason: Optional[str] = None


class CallAllocator:
    """
    Executes a single approved call allocation.

    Usage
    -----
        allocator = CallAllocator(store)
        result = allocator.allocate(request, provider, event_callback)
        if result.success:
            print(f"Call {result.call.id} initiated")
        else:
            print(f"Allocation failed: {result.failure_reason}")
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def allocate(
        self,
        request: AllocationRequest,
        provider: TelecomProvider,
        event_callback: Optional[EventCallback] = None,
    ) -> AllocationResult:
        """
        Attempt to allocate and initiate one outbound call.

        Steps
        -----
        1. Pre-flight check: is the provider healthy?
        2. Generate a shared reservation_id (used to correlate agent, borrower,
           and call records — the recovery process uses this to clean up).
        3. Atomically reserve the agent.
        4. Atomically reserve the borrower.  On failure: release agent.
        5. Create the Call record in QUEUED state.
        6. Transition call to RESERVED.
        7. Link agent to call (set agent.call_id, agent.borrower_id).
        8. Initiate through the provider.  On failure: clean up all three.
        9. On success: move agent to DIALING, call to INITIATED.
        10. Persist all state.
        """
        agent_id = request.agent_id
        borrower_id = request.borrower_id

        # ------------------------------------------------------------------
        # Step 1: Provider health guard
        # ------------------------------------------------------------------
        if not provider.is_healthy():
            logger.warning(
                "Allocation skipped: provider %s is not healthy", provider.name
            )
            return AllocationResult(
                success=False,
                agent_id=agent_id,
                borrower_id=borrower_id,
                failure_reason=f"provider {provider.name!r} is not healthy",
            )

        # ------------------------------------------------------------------
        # Step 2: Shared reservation ID
        # ------------------------------------------------------------------
        reservation_id = str(uuid.uuid4())
        lease_seconds = request.lease_seconds
        deadline = datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)

        # ------------------------------------------------------------------
        # Step 3: Reserve the agent
        # ------------------------------------------------------------------
        agent_reserved = self._store.atomic_reserve_agent(
            agent_id, reservation_id, lease_seconds
        )
        if not agent_reserved:
            logger.debug(
                "Allocation failed: agent %s could not be reserved (taken by another worker)",
                agent_id[:8],
            )
            return AllocationResult(
                success=False,
                agent_id=agent_id,
                borrower_id=borrower_id,
                failure_reason="agent already reserved by another worker",
            )

        # ------------------------------------------------------------------
        # Step 4: Reserve the borrower
        # ------------------------------------------------------------------
        borrower_reserved = self._store.atomic_reserve_borrower(
            borrower_id, reservation_id
        )
        if not borrower_reserved:
            # Clean up the agent reservation we just made.
            self._store.release_agent(agent_id)
            logger.debug(
                "Allocation failed: borrower %s could not be reserved; agent released",
                borrower_id[:8],
            )
            return AllocationResult(
                success=False,
                agent_id=agent_id,
                borrower_id=borrower_id,
                failure_reason="borrower already reserved by another worker",
            )

        # ------------------------------------------------------------------
        # Step 5: Create the Call record
        # ------------------------------------------------------------------
        call = Call(
            agent_id=agent_id,
            borrower_id=borrower_id,
            campaign_id=request.campaign_id,
            provider=provider.name,
            reservation_id=reservation_id,
            lease_until=deadline,
        )

        # ------------------------------------------------------------------
        # Step 6: Transition call to RESERVED
        # ------------------------------------------------------------------
        call.apply_transition(CallState.RESERVED, event_id=f"{call.id}-reserved")

        # ------------------------------------------------------------------
        # Step 7: Link agent to this call
        # ------------------------------------------------------------------
        agent = self._store.get_agent(agent_id)
        if agent is None:
            # Should never happen, but defend against it.
            self._store.release_borrower(borrower_id)
            return AllocationResult(
                success=False,
                agent_id=agent_id,
                borrower_id=borrower_id,
                failure_reason="agent disappeared from store mid-allocation",
            )
        agent.borrower_id = borrower_id
        agent.call_id = call.id

        # Save call now (RESERVED state) before hitting the provider.
        # This ensures that even if the process crashes after this point,
        # the recovery process can find the call and clean it up.
        self._store.save_call(call)
        self._store.save_agent(agent)

        # ------------------------------------------------------------------
        # Step 8: Initiate through the provider
        # ------------------------------------------------------------------
        # Use a no-op callback if the caller didn't supply one.
        callback = event_callback or (lambda event: None)

        provider_accepted = provider.initiate_call(call, callback)

        if not provider_accepted:
            # The provider refused.  Roll back everything.
            call.apply_transition(
                CallState.FAILED, event_id=f"{call.id}-provider-rejected"
            )
            call.failure_reason = f"provider {provider.name!r} rejected the call"
            self._store.save_call(call)
            self._store.release_agent(agent_id)
            self._store.release_borrower(borrower_id)
            logger.warning(
                "Provider %s rejected call %s; resources released",
                provider.name,
                call.id[:8],
            )
            return AllocationResult(
                success=False,
                call=call,
                agent_id=agent_id,
                borrower_id=borrower_id,
                failure_reason=f"provider {provider.name!r} rejected the call",
            )

        # ------------------------------------------------------------------
        # Step 9: Provider accepted — move to DIALING / INITIATED
        # ------------------------------------------------------------------
        call.apply_transition(CallState.INITIATED, event_id=f"{call.id}-initiated")

        # Move agent from RESERVED → DIALING.
        agent.state = AgentState.DIALING

        # Save final state.
        self._store.save_call(call)
        self._store.save_agent(agent)

        logger.info(
            "Call %s initiated: agent=%s borrower=%s provider=%s",
            call.id[:8],
            agent_id[:8],
            borrower_id[:8],
            provider.name,
        )

        return AllocationResult(
            success=True,
            call=call,
            agent_id=agent_id,
            borrower_id=borrower_id,
        )

    def bulk_allocate(
        self,
        requests: list[AllocationRequest],
        provider: TelecomProvider,
        event_callback: Optional[EventCallback] = None,
    ) -> list[AllocationResult]:
        """
        Execute multiple allocation requests in sequence.

        Used by the progressive dialer and (after Phase 5) by the predictive
        pacing engine after the Safety Controller has approved N calls.

        Each request is independent: a failure in one does not abort the others.
        """
        results = []
        for req in requests:
            result = self.allocate(req, provider, event_callback)
            results.append(result)
        return results
