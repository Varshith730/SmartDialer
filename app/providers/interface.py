"""
app/providers/interface.py
--------------------------
Abstract interface that every telecom provider must implement.

Problem this solves:
    The Call Allocator needs to place outbound calls, but must not know
    *how* Provider A or Provider B works internally.  By depending only
    on this interface, the allocator is fully decoupled from any specific
    provider SDK or protocol.

    This also lets us swap in a NullProvider (for unit tests) or a
    SimulatedProvider (for simulation) without touching any allocator code.

Architecture boundary:
    The Predictive Pacing Engine is NOT allowed to call this interface.
    Only the Call Allocator may do so, and only after the Safety Controller
    has approved the call count.

Provider contract:
    - initiate_call() must be non-blocking in the happy path.
    - Events (RINGING, ANSWERED, COMPLETED, FAILED …) are delivered
      asynchronously via the event_callback.
    - The callback receives a ProviderEvent; the caller is responsible for
      routing it to the event processor.
    - is_healthy() must be cheap — it is polled frequently by the Safety
      Controller and the circuit breaker.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from app.models.call import Call


@dataclass
class ProviderEvent:
    """
    A single event emitted by a telecom provider about an outbound call.

    Fields
    ------
    event_id    Stable, globally-unique identifier for this delivery.
                Used for idempotency: the same physical event delivered
                twice must carry the same event_id.
    call_id     Which call this event refers to.
    event_type  String name matching a CallState (e.g. "RINGING", "COMPLETED").
    timestamp   When the event was generated at the provider side.
    metadata    Optional bag of extra provider-specific data.
    """

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    call_id: str = ""
    event_type: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ProviderEvent(id={self.event_id[:8]}…, "
            f"call={self.call_id[:8]}…, type={self.event_type})"
        )


# Type alias for the async event callback.
# The allocator/event-processor registers this callback with the provider.
EventCallback = Callable[[ProviderEvent], None]


class TelecomProvider(ABC):
    """
    Abstract base class for all telecom providers.

    Concrete implementations:
        ProviderA  — fast, reliable (Phases 6+)
        ProviderB  — slow, unreliable, duplicate/out-of-order events (Phases 6+)
        NullProvider — no-op for unit tests (this file)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier used in logging and call records."""
        ...

    @abstractmethod
    def is_healthy(self) -> bool:
        """
        Return True if the provider is currently accepting new calls.

        This is polled by the Safety Controller and circuit breaker.
        Must be cheap (no network I/O in production; just check cached state).
        """
        ...

    @abstractmethod
    def initiate_call(self, call: Call, event_callback: EventCallback) -> bool:
        """
        Ask the provider to place an outbound call.

        Parameters
        ----------
        call            The Call record.  Read call.id, call.borrower_id etc.
        event_callback  Function to call when a provider event arrives.
                        The provider must call this with a ProviderEvent.

        Returns True if the call was successfully handed off to the provider,
        False if the provider rejected it (busy, unhealthy, etc.).

        This method should return quickly.  Provider events (RINGING, ANSWERED,
        COMPLETED, FAILED) are delivered later via event_callback.
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"


# ---------------------------------------------------------------------------
# NullProvider — used in unit tests and Phase 2 allocation tests
# ---------------------------------------------------------------------------

class NullProvider(TelecomProvider):
    """
    A provider that does nothing but record what it was asked to do.

    Useful for:
    - Unit tests that want to test allocation logic without real call flow.
    - Verifying that the allocator calls the provider exactly once per call.
    - Simulating an unhealthy provider (set healthy=False).
    """

    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy
        # Records all initiate_call invocations for assertion in tests.
        self.initiated_calls: list[str] = []

    @property
    def name(self) -> str:
        return "null_provider"

    def is_healthy(self) -> bool:
        return self._healthy

    def set_healthy(self, value: bool) -> None:
        self._healthy = value

    def initiate_call(self, call: Call, event_callback: EventCallback) -> bool:
        if not self._healthy:
            return False
        self.initiated_calls.append(call.id)
        # NullProvider does not emit any events.
        # Real providers would schedule async event delivery here.
        return True
