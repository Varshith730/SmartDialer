"""
app/providers/provider_a.py
----------------------------
Provider A — fast, reliable telecom provider.

Characteristics:
    * High answer rate (configurable, default 80%)
    * Low failure rate (configurable, default 2%)
    * Normal, ordered event sequence
    * Short ring latency

Event sequence (happy path):
    INITIATED (by allocator)
    → RINGING     (provider reports the phone is ringing)
    → ANSWERED    (borrower picks up)
    → CONNECTED   (voice path established)
    → COMPLETED   (call ends normally)

Event sequence (no answer):
    → RINGING
    → FAILED      (no answer / busy / rejected)

Design:
    Events are delivered asynchronously via a daemon thread.
    The `delay_scale` parameter multiplies all delays — set to 0.0
    in tests for instant synchronous delivery, 1.0 for realistic timing.

    The circuit breaker integration is the CALLER's responsibility:
    the provider does not know about the circuit breaker.  When
    `initiate_call` returns False (simulated failure), the caller
    (the allocator) should call circuit_breaker.record_failure().
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from datetime import datetime, timezone

from app.models.call import Call
from app.providers.interface import EventCallback, ProviderEvent, TelecomProvider

logger = logging.getLogger(__name__)


class ProviderA(TelecomProvider):
    """
    Reliable mock telecom provider.

    Parameters
    ----------
    answer_rate     Probability a call is answered (0.0 – 1.0).
    failure_rate    Probability initiate_call() itself fails (simulates
                    API errors, before any event is delivered).
    ring_time       Seconds before RINGING is delivered.
    answer_time     Additional seconds from RINGING to ANSWERED.
    connect_time    Additional seconds from ANSWERED to CONNECTED.
    talk_time       Seconds the call stays CONNECTED before COMPLETED.
    delay_scale     Multiplier for all delays. Set to 0.0 in tests.
    seed            Optional RNG seed for reproducible tests.
    """

    def __init__(
        self,
        answer_rate: float = 0.80,
        failure_rate: float = 0.02,
        ring_time: float = 2.0,
        answer_time: float = 0.5,
        connect_time: float = 0.2,
        talk_time: float = 60.0,
        delay_scale: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self._answer_rate = answer_rate
        self._failure_rate = failure_rate
        self._ring_time = ring_time
        self._answer_time = answer_time
        self._connect_time = connect_time
        self._talk_time = talk_time
        self._delay_scale = delay_scale
        self._rng = random.Random(seed)

        # Track all events delivered (for testing assertions).
        self._events_delivered: list[ProviderEvent] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # TelecomProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "provider_a"

    def is_healthy(self) -> bool:
        return True  # Provider A is always healthy in this prototype.

    def initiate_call(self, call: Call, event_callback: EventCallback) -> bool:
        """
        Accept the call and begin async event delivery in a daemon thread.

        Returns False with `failure_rate` probability to simulate provider
        API errors (the call never starts ringing).
        """
        with self._lock:
            if self._rng.random() < self._failure_rate:
                logger.warning("[ProviderA] initiate_call REJECTED for call %s", call.id[:8])
                return False

        # Spin up a daemon thread so the call events are delivered asynchronously.
        # Daemon=True means the thread won't block process exit.
        thread = threading.Thread(
            target=self._deliver_events,
            args=(call.id, event_callback),
            daemon=True,
            name=f"provider_a-{call.id[:8]}",
        )
        thread.start()
        return True

    # ------------------------------------------------------------------
    # Internal: event delivery loop
    # ------------------------------------------------------------------

    def _deliver_events(self, call_id: str, callback: EventCallback) -> None:
        """Deliver events for a single call in sequence."""
        try:
            # RINGING
            self._sleep(self._ring_time)
            self._emit(callback, call_id, "RINGING")

            with self._lock:
                answered = self._rng.random() < self._answer_rate

            if not answered:
                # No answer → fail the call.
                self._sleep(self._answer_time)
                self._emit(callback, call_id, "FAILED")
                return

            # ANSWERED
            self._sleep(self._answer_time)
            self._emit(callback, call_id, "ANSWERED")

            # CONNECTED
            self._sleep(self._connect_time)
            self._emit(callback, call_id, "CONNECTED")

            # COMPLETED (after talk time)
            self._sleep(self._talk_time)
            self._emit(callback, call_id, "COMPLETED")

        except Exception as exc:  # noqa: BLE001
            logger.error("[ProviderA] Error delivering events for %s: %s", call_id[:8], exc)
            self._emit(callback, call_id, "FAILED")

    def _emit(self, callback: EventCallback, call_id: str, event_type: str) -> None:
        """Build a ProviderEvent, record it, and deliver to the callback."""
        event = ProviderEvent(
            event_id=str(uuid.uuid4()),
            call_id=call_id,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
        )
        with self._lock:
            self._events_delivered.append(event)
        logger.debug("[ProviderA] Event %s → call %s", event_type, call_id[:8])
        callback(event)

    def _sleep(self, seconds: float) -> None:
        """Sleep with the delay_scale applied. scale=0.0 → instant."""
        scaled = seconds * self._delay_scale
        if scaled > 0:
            time.sleep(scaled)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @property
    def events_delivered(self) -> list[ProviderEvent]:
        """All events delivered so far (in delivery order)."""
        with self._lock:
            return list(self._events_delivered)

    def event_types_for(self, call_id: str) -> list[str]:
        """Return the list of event_type strings delivered for a given call."""
        with self._lock:
            return [e.event_type for e in self._events_delivered if e.call_id == call_id]
