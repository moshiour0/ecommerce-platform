"""
Circuit breaker + bulkhead for synchronous inter-service calls (Rule 11).

The Node side (shared/libs/node-common/resilience.js) implements the same
contract: open after 5 *consecutive* downstream failures, stay open 30s, then
allow a single half-open probe. Both runtimes are hand-rolled against that
spec rather than delegating to a library's own policy model, because the
library defaults do not express it -- opossum is percentage-over-a-window and
silently never trips at a 100% threshold, and Python's `circuitbreaker` is
built around sync decorators. One specification, two faithful implementations,
is worth more than two libraries that each approximate it differently.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

FAILURE_THRESHOLD = 5
RESET_TIMEOUT_SECONDS = 30.0
DEFAULT_BULKHEAD = 10


class CircuitOpenError(Exception):
    """Raised instead of calling a downstream whose circuit is open."""

    def __init__(self, name: str, retry_in: float):
        super().__init__(f"Circuit open for {name}; retry in {retry_in:.1f}s")
        self.name = name
        self.retry_in = retry_in


class BulkheadFullError(Exception):
    """Raised when a downstream already has its maximum calls in flight."""

    def __init__(self, name: str, limit: int):
        super().__init__(f"Bulkhead full for {name} (limit {limit})")
        self.name = name
        self.limit = limit


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def is_downstream_failure(exc: BaseException) -> bool:
    """Decide whether an exception indicates an unhealthy dependency.

    A 4xx means the downstream answered correctly and rejected the request --
    it must never open a circuit, or a burst of 404s would take out a healthy
    service. Only 5xx, timeouts and transport errors count.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status >= 500
    return True  # timeout, connection refused, DNS, etc.


class AsyncCircuitBreaker:
    """Async circuit breaker with an attached bulkhead.

    The bulkhead bounds concurrent in-flight calls and rejects immediately
    when saturated rather than queueing; an unbounded queue only relocates
    the starvation it was meant to prevent.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = FAILURE_THRESHOLD,
        reset_timeout: float = RESET_TIMEOUT_SECONDS,
        bulkhead: int = DEFAULT_BULKHEAD,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.bulkhead_limit = bulkhead

        self._state = State.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(bulkhead)
        self._in_flight = 0

        self.total_calls = 0
        self.total_failures = 0
        self.total_short_circuited = 0

    @property
    def state(self) -> str:
        return self._state.value

    async def _before_call(self) -> None:
        """Admission control. Raises if the call must not proceed."""
        async with self._lock:
            if self._state is State.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed < self.reset_timeout:
                    self.total_short_circuited += 1
                    raise CircuitOpenError(self.name, self.reset_timeout - elapsed)
                # Reset window elapsed: allow exactly one probe through.
                self._state = State.HALF_OPEN
                self._probe_in_flight = True
                logger.warning("Circuit HALF-OPEN for %s — probing with a single request", self.name)
                return

            if self._state is State.HALF_OPEN:
                if self._probe_in_flight:
                    self.total_short_circuited += 1
                    raise CircuitOpenError(self.name, 0.0)
                self._probe_in_flight = True

    async def _on_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0
            self._probe_in_flight = False
            if self._state is not State.CLOSED:
                self._state = State.CLOSED
                logger.info("Circuit CLOSED for %s — downstream healthy again", self.name)

    async def _on_failure(self, exc: BaseException) -> None:
        if not is_downstream_failure(exc):
            # The downstream answered; this is not evidence of ill health.
            async with self._lock:
                self._probe_in_flight = False
            return

        async with self._lock:
            self.total_failures += 1
            if self._state is State.HALF_OPEN:
                # The probe failed: straight back to open, without waiting to
                # re-accumulate failures while the dependency is still down.
                self._probe_in_flight = False
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                logger.error("Circuit re-OPEN for %s — half-open probe failed", self.name)
                return

            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = State.OPEN
                self._opened_at = time.monotonic()
                self._consecutive_failures = 0
                logger.error(
                    "Circuit OPEN for %s — %s consecutive failures, failing fast for %ss",
                    self.name, self.failure_threshold, self.reset_timeout,
                )

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        await self._before_call()

        if self._semaphore.locked() and self._in_flight >= self.bulkhead_limit:
            raise BulkheadFullError(self.name, self.bulkhead_limit)

        async with self._semaphore:
            self._in_flight += 1
            self.total_calls += 1
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                await self._on_failure(exc)
                raise
            else:
                await self._on_success()
                return result
            finally:
                self._in_flight -= 1

    def stats(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "consecutive_failures": self._consecutive_failures,
            "in_flight": self._in_flight,
            "bulkhead_limit": self.bulkhead_limit,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "total_short_circuited": self.total_short_circuited,
        }
