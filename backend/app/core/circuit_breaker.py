"""A small in-process circuit breaker for an unreliable dependency (here: the API's Redis).

CLOSED     calls go through; `failure_threshold` consecutive failures (errors or timeouts)
           -> OPEN.
OPEN       calls are rejected immediately with CircuitOpenError, without touching the
           dependency, for `open_seconds`. The first call after that -> HALF_OPEN.
HALF_OPEN  exactly one trial call goes through (others are rejected): success -> CLOSED,
           failure -> OPEN again for another `open_seconds`.

Only state transitions are logged, never individual rejected calls. State lives in the
process: each API worker process has its own breaker.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RedisError):
    """The call was skipped because the circuit is open. Subclasses RedisError so code that
    already degrades on Redis errors degrades on this too."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int,
        open_seconds: float,
        call_timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._failure_threshold = failure_threshold
        self._open_seconds = open_seconds
        self._call_timeout = call_timeout_seconds
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        return self._state

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run `operation` under the breaker. Raises CircuitOpenError when skipped; other
        errors from the operation propagate after being counted."""
        self._admit()
        try:
            # Belt and braces: the client has socket timeouts too, but a call must never
            # hang longer than this, whatever the client does.
            async with asyncio.timeout(self._call_timeout):
                result = await operation()
        except (RedisError, OSError) as exc:  # includes TimeoutError (an OSError)
            self._record_failure(exc)
            raise
        except BaseException:
            # Not a dependency failure (a bug, or cancellation): don't count it, but don't
            # leave a half-open trial slot stuck either.
            self._trial_in_flight = False
            raise
        self._record_success()
        return result

    def _admit(self) -> None:
        if self._state is BreakerState.OPEN:
            if self._clock() - self._opened_at < self._open_seconds:
                raise CircuitOpenError(f"{self._name} circuit is open")
            self._transition(BreakerState.HALF_OPEN, "open period elapsed, trying one call")
        if self._state is BreakerState.HALF_OPEN:
            if self._trial_in_flight:
                raise CircuitOpenError(f"{self._name} circuit is half-open, trial in flight")
            self._trial_in_flight = True

    def _record_failure(self, exc: BaseException) -> None:
        if self._state is BreakerState.HALF_OPEN:
            self._trial_in_flight = False
            self._open(f"trial call failed: {exc!r}")
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open(f"{self._failures} consecutive failures, last: {exc!r}")

    def _record_success(self) -> None:
        self._failures = 0
        if self._state is BreakerState.HALF_OPEN:
            self._trial_in_flight = False
            self._transition(BreakerState.CLOSED, "trial call succeeded")

    def _open(self, reason: str) -> None:
        self._opened_at = self._clock()
        self._failures = 0
        self._transition(BreakerState.OPEN, reason)

    def _transition(self, state: BreakerState, reason: str) -> None:
        previous, self._state = self._state, state
        level = logging.INFO if state is BreakerState.CLOSED else logging.WARNING
        logger.log(
            level,
            "circuit breaker state change",
            extra={
                "breaker": self._name,
                "from_state": previous.value,
                "to_state": state.value,
                "reason": reason,
                "open_seconds": self._open_seconds,
            },
        )
