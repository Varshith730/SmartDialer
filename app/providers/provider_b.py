"""
app/providers/provider_b.py
----------------------------
Provider B — slow, unreliable telecom provider.

This provider intentionally misbehaves to stress-test the system's
idempotency and out-of-order event handling.

Characteristics:
    * Lower answer rate than Provider A
    * Higher failure rate
    * Occasional event duplication (ANSWERED ANSWERED ANSWERED)
    * Occasional out-of-order events (COMPLETED before RINGING)
    * Occasional timeouts (no COMPLETED ever arrives)
    * Longer delays

These are NOT bugs in our system — they are expected behaviours of
real telecom networks.  The system must remain consistent regardless.

The spec requires us to generate cases such as:
    ANSWERED  ANSWERED  ANSWERED  COMPLETED
and:
    COMPLETED  ANSWERED  RINGING

Both cases must be handled safely by the event processor, which
delegates to call.apply_transition() — the idempotency/ordering guard.

Chaos parameters:
    duplicate_probability   Chance an event is delivered multiple times.
    duplicate_count         How many extra copies to send.
    out_of_order_probability  Chance the event sequence is scrambled.
    timeout_probability     Chance COMPLETED is never sent (call hangs).
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


class ProviderB(TelecomProvider):
    """
    Chaotic mock telecom provider.

    Parameters
    ----------
    answer_rate             Probability a call is answered.
    failure_rate            Probability initiate_call() fails outright.
    duplicate_probability   Probability any single event is duplicated.
    duplicate_count         How many duplicate copies to send.
    out_of_order_probability  Probability events are scrambled before delivery.
    timeout_probability     Probability COMPLETED never arrives (timeout).
    ring_time, talk_time    Base timings (scaled by delay_scale).
    delay_scale             0.0 for instant test delivery.
    seed                    Optional RNG seed.
    """

    def __init__(
        self,
        answer_rate: float = 0.50,
        failure_rate: float = 0.10,
        duplicate_probability: float = 0.30,
        duplicate_count: int = 2,
        out_of_order_probability: float = 0.20,
        timeout_probability: float = 0.05,
        ring_time: float = 4.0,
        talk_time: float = 90.0,
        delay_scale: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self._answer_rate = answer_rate
        self._failure_rate = failure_rate
        self._duplicate_probability = duplicate_probability
        self._duplicate_count = duplicate_count
        self._out_of_order_probability = out_of_order_probability
        self._timeout_probability = timeout_probability
        self._ring_time = ring_time
        self._talk_time = talk_time
        self._delay_scale = delay_scale
        self._rng = random.Random(seed)

        self._events_delivered: list[ProviderEvent] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # TelecomProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "provider_b"

    def is_healthy(self) -> bool:
        return True  # Provider B is alive; it's just unreliable.

    def initiate_call(self, call: Call, event_callback: EventCallback) -> bool:
        with self._lock:
            if self._rng.random() < self._failure_rate:
                logger.warning("[ProviderB] initiate_call REJECTED for call %s", call.id[:8])
                return False

        thread = threading.Thread(
            target=self._deliver_events,
            args=(call.id, event_callback),
            daemon=True,
            name=f"provider_b-{call.id[:8]}",
        )
        thread.start()
        return True

    # ------------------------------------------------------------------
    # Internal: chaotic event delivery
    # ------------------------------------------------------------------

    def _deliver_events(self, call_id: str, callback: EventCallback) -> None:
        """
        Build the event sequence, then inject chaos before delivery.

        Chaos injections (each independently probabilistic):
        1. Out-of-order: shuffle the event list.
        2. Duplication: for each event, sometimes send it multiple times
           with the SAME event_id (idempotency test).
        3. Timeout: skip the COMPLETED event entirely.
        """
        try:
            with self._lock:
                answered = self._rng.random() < self._answer_rate
                out_of_order = self._rng.random() < self._out_of_order_probability
                timeout = self._rng.random() < self._timeout_probability

            # Build the intended event sequence (as (event_type, event_id) pairs).
            # We generate event_ids here so duplicates can reuse the same id.
            if answered:
                sequence = [
                    ("RINGING",   str(uuid.uuid4())),
                    ("ANSWERED",  str(uuid.uuid4())),
                    ("CONNECTED", str(uuid.uuid4())),
                    ("COMPLETED", str(uuid.uuid4())),
                ]
            else:
                sequence = [
                    ("RINGING", str(uuid.uuid4())),
                    ("FAILED",  str(uuid.uuid4())),
                ]

            # Chaos 1: out-of-order — shuffle the sequence.
            if out_of_order:
                with self._lock:
                    shuffled = list(sequence)
                    self._rng.shuffle(shuffled)
                sequence = shuffled
                logger.debug("[ProviderB] OUT-OF-ORDER events for call %s", call_id[:8])

            # Chaos 3: timeout — drop the COMPLETED event.
            if timeout and answered:
                sequence = [s for s in sequence if s[0] != "COMPLETED"]
                logger.debug("[ProviderB] TIMEOUT (no COMPLETED) for call %s", call_id[:8])

            # Deliver events, injecting duplicates as we go.
            for event_type, event_id in sequence:
                self._sleep(self._ring_time * 0.3)  # small gap between events

                # Emit the real event.
                self._emit(callback, call_id, event_type, event_id)

                # Chaos 2: duplication — re-send with the SAME event_id.
                with self._lock:
                    should_dup = self._rng.random() < self._duplicate_probability
                if should_dup:
                    for _ in range(self._duplicate_count):
                        self._sleep(0.01 * self._delay_scale)
                        logger.debug(
                            "[ProviderB] DUPLICATE %s for call %s (event_id=%s)",
                            event_type, call_id[:8], event_id[:8],
                        )
                        self._emit(callback, call_id, event_type, event_id)

        except Exception as exc:  # noqa: BLE001
            logger.error("[ProviderB] Error delivering events for %s: %s", call_id[:8], exc)
            self._emit(callback, call_id, "FAILED", str(uuid.uuid4()))

    def _emit(
        self,
        callback: EventCallback,
        call_id: str,
        event_type: str,
        event_id: str | None = None,
    ) -> None:
        """Build and deliver a ProviderEvent, recording it for assertions."""
        event = ProviderEvent(
            event_id=event_id or str(uuid.uuid4()),
            call_id=call_id,
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
        )
        with self._lock:
            self._events_delivered.append(event)
        logger.debug("[ProviderB] Event %s → call %s", event_type, call_id[:8])
        callback(event)

    def _sleep(self, seconds: float) -> None:
        scaled = seconds * self._delay_scale
        if scaled > 0:
            time.sleep(scaled)

    # ------------------------------------------------------------------
    # Chaos injection helpers (for deterministic testing)
    # ------------------------------------------------------------------

    def inject_duplicate(
        self,
        call_id: str,
        event_type: str,
        event_id: str,
        callback: EventCallback,
        count: int = 3,
    ) -> None:
        """
        Manually inject `count` copies of the same event.
        Used in tests to precisely control duplication scenarios.
        """
        for _ in range(count):
            self._emit(callback, call_id, event_type, event_id)

    def inject_out_of_order(
        self,
        call_id: str,
        events: list[tuple[str, str]],   # [(event_type, event_id), ...]
        callback: EventCallback,
    ) -> None:
        """
        Deliver a custom sequence of events in the given order.
        Used in tests to precisely control out-of-order scenarios.
        """
        for event_type, event_id in events:
            self._emit(callback, call_id, event_type, event_id)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    @property
    def events_delivered(self) -> list[ProviderEvent]:
        with self._lock:
            return list(self._events_delivered)

    def event_types_for(self, call_id: str) -> list[str]:
        with self._lock:
            return [e.event_type for e in self._events_delivered if e.call_id == call_id]
