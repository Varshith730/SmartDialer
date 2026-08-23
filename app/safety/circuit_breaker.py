"""
app/safety/circuit_breaker.py
------------------------------
Circuit Breaker for telecom provider health tracking.

Problem this solves:
    When a provider starts failing (timeouts, errors), we should stop
    hammering it with new calls.  The circuit breaker automatically opens
    after too many consecutive failures, giving the provider time to recover.
    Once cooled down, it allows a limited probe before re-opening fully.

State machine:
    CLOSED     Normal operation.  Calls are allowed.
               Failure count increments on each failure.
               Transitions to OPEN when failure_threshold is exceeded.

    OPEN       Provider is down.  All new calls are rejected.
               After cooldown_seconds, transitions to HALF_OPEN.

    HALF_OPEN  Recovery probe state.  Allows exactly one call through.
               If that call succeeds → back to CLOSED.
               If it fails → back to OPEN (restart cooldown).

Why a circuit breaker?
    Without one, a degraded provider causes a flood of failures: agents
    get reserved, calls are initiated, the provider times out, resources
    are released, and the cycle repeats at full speed.  The circuit breaker
    short-circuits that loop immediately on OPEN, protecting agents and
    preserving system stability.

Thread safety:
    All state transitions use a threading.Lock so that the circuit breaker
    is safe to use from multiple dialler threads simultaneously.
"""

from __future__ import annotations

import enum
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitState(str, enum.Enum):
    """The three possible circuit breaker states."""
    CLOSED    = "CLOSED"     # Normal: calls permitted.
    OPEN      = "OPEN"       # Tripped: calls rejected.
    HALF_OPEN = "HALF_OPEN"  # Probing: one call permitted.


class CircuitBreaker:
    """
    Thread-safe circuit breaker for a single telecom provider.

    Parameters
    ----------
    provider_name       : Name used in log messages.
    failure_threshold   : Consecutive failures needed to open the circuit.
    cooldown_seconds    : How long to stay OPEN before trying HALF_OPEN.
    success_threshold   : Consecutive successes in HALF_OPEN to close.

    Usage
    -----
        cb = CircuitBreaker("provider_a", failure_threshold=3, cooldown_seconds=30)

        if cb.is_call_permitted():
            success = try_call()
            if success:
                cb.record_success()
            else:
                cb.record_failure()
        else:
            # Circuit is OPEN — skip this provider.
            pass
    """

    def __init__(
        self,
        provider_name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        success_threshold: int = 1,
    ) -> None:
        self.provider_name = provider_name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._opened_at: Optional[datetime] = None
        self._lock = threading.Lock()

        # Tracking for the Safety Controller and reporting.
        self._total_failures: int = 0
        self._total_successes: int = 0
        self._total_calls_permitted: int = 0

    # ------------------------------------------------------------------
    # Public read-only properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Current circuit state (thread-safe read)."""
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    def is_open(self) -> bool:
        """Return True if the circuit is OPEN (calls NOT permitted)."""
        return self.state == CircuitState.OPEN

    def is_closed(self) -> bool:
        """Return True if the circuit is CLOSED (normal operation)."""
        return self.state == CircuitState.CLOSED

    def is_half_open(self) -> bool:
        """Return True if the circuit is HALF_OPEN (probe in progress)."""
        return self.state == CircuitState.HALF_OPEN

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def is_call_permitted(self) -> bool:
        """
        Check whether a new call attempt is allowed.

        CLOSED     → always permitted.
        OPEN       → never permitted (unless cooldown has elapsed).
        HALF_OPEN  → permitted for the first caller only (acts as the probe).

        Thread safety: multiple threads calling this simultaneously will each
        get an accurate answer.  In HALF_OPEN, the first thread to call this
        wins the probe slot; subsequent callers see OPEN and return False.
        """
        with self._lock:
            self._maybe_transition_to_half_open()

            if self._state == CircuitState.CLOSED:
                self._total_calls_permitted += 1
                return True

            if self._state == CircuitState.OPEN:
                return False

            # HALF_OPEN: allow exactly one probe call.
            # We transition back to OPEN immediately so that a second concurrent
            # caller does not also get a probe slot.
            self._state = CircuitState.OPEN
            self._opened_at = datetime.now(timezone.utc)
            self._total_calls_permitted += 1
            logger.info("[CircuitBreaker:%s] Probe call permitted (HALF_OPEN → OPEN)", self.provider_name)
            return True

    def record_success(self) -> None:
        """
        Record a successful call outcome.

        In HALF_OPEN (actually temporarily back to OPEN after the probe):
            After probe succeeds, close the circuit.
        In CLOSED:
            Reset the failure counter.
        """
        with self._lock:
            self._total_successes += 1
            self._consecutive_failures = 0
            self._consecutive_successes += 1

            # If we were probing (circuit was OPEN after half-open probe) and
            # the probe succeeded, close the circuit.
            if self._state == CircuitState.OPEN and self._consecutive_successes >= self.success_threshold:
                self._close_circuit()
            elif self._state == CircuitState.CLOSED:
                pass  # Normal success — nothing to do beyond resetting above.

    def record_failure(self) -> None:
        """
        Record a failed call outcome.

        If consecutive failures reach the threshold, open the circuit.
        If a probe (HALF_OPEN) fails, re-open the circuit and restart cooldown.
        """
        with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1
            self._consecutive_successes = 0

            if self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    self._open_circuit()
            elif self._state == CircuitState.OPEN:
                # Probe failed (we set state to OPEN when issuing the probe).
                # Reset the cooldown timer.
                self._opened_at = datetime.now(timezone.utc)
                logger.warning(
                    "[CircuitBreaker:%s] Probe failed — restarting cooldown (%ds)",
                    self.provider_name, self.cooldown_seconds,
                )

    # ------------------------------------------------------------------
    # Manual controls (for testing and emergency use)
    # ------------------------------------------------------------------

    def force_open(self) -> None:
        """Manually open the circuit (for testing or emergency stop)."""
        with self._lock:
            self._open_circuit()

    def force_close(self) -> None:
        """Manually close the circuit (for testing or post-maintenance)."""
        with self._lock:
            self._close_circuit()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Return a snapshot of the circuit breaker's current statistics."""
        with self._lock:
            return {
                "provider": self.provider_name,
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "consecutive_successes": self._consecutive_successes,
                "total_failures": self._total_failures,
                "total_successes": self._total_successes,
                "total_permitted": self._total_calls_permitted,
                "opened_at": self._opened_at.isoformat() if self._opened_at else None,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open_circuit(self) -> None:
        """Transition to OPEN state and record when it happened."""
        self._state = CircuitState.OPEN
        self._opened_at = datetime.now(timezone.utc)
        logger.error(
            "[CircuitBreaker:%s] Circuit OPENED after %d consecutive failures",
            self.provider_name, self._consecutive_failures,
        )

    def _close_circuit(self) -> None:
        """Transition to CLOSED state and reset all counters."""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at = None
        logger.info("[CircuitBreaker:%s] Circuit CLOSED — provider recovered", self.provider_name)

    def _maybe_transition_to_half_open(self) -> None:
        """
        If OPEN and the cooldown has elapsed, move to HALF_OPEN.

        Must be called while holding self._lock.
        """
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            elapsed = (datetime.now(timezone.utc) - self._opened_at).total_seconds()
            if elapsed >= self.cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                logger.info(
                    "[CircuitBreaker:%s] Cooldown elapsed (%.1fs) — entering HALF_OPEN",
                    self.provider_name, elapsed,
                )

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(provider={self.provider_name!r}, "
            f"state={self._state.value}, failures={self._consecutive_failures})"
        )
