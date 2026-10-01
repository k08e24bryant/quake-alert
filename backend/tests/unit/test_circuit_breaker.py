import asyncio
import logging

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.circuit_breaker import BreakerState, CircuitBreaker, CircuitOpenError

OPEN_SECONDS = 30.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Dependency:
    """A stand-in for Redis that counts calls and fails, hangs or succeeds on demand."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "fail"

    async def __call__(self) -> str:
        self.calls += 1
        if self.mode == "fail":
            raise RedisConnectionError("connection refused")
        if self.mode == "hang":
            await asyncio.sleep(10)
        return "ok"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(
        name="redis",
        failure_threshold=3,
        open_seconds=OPEN_SECONDS,
        call_timeout_seconds=0.05,
        clock=clock,
    )


def state(breaker: CircuitBreaker) -> BreakerState:
    """Read the state through a call so mypy doesn't narrow it across awaits."""
    return breaker.state


async def fail_times(breaker: CircuitBreaker, dependency: Dependency, n: int) -> None:
    for _ in range(n):
        with pytest.raises(RedisConnectionError):
            await breaker.call(dependency)


async def test_opens_after_threshold_consecutive_failures(breaker: CircuitBreaker) -> None:
    dependency = Dependency()

    await fail_times(breaker, dependency, 2)
    assert state(breaker) is BreakerState.CLOSED
    await fail_times(breaker, dependency, 1)
    assert state(breaker) is BreakerState.OPEN


async def test_open_circuit_skips_the_dependency(breaker: CircuitBreaker) -> None:
    dependency = Dependency()
    await fail_times(breaker, dependency, 3)

    for _ in range(10):
        with pytest.raises(CircuitOpenError):
            await breaker.call(dependency)

    assert dependency.calls == 3


async def test_success_resets_the_consecutive_failure_count(breaker: CircuitBreaker) -> None:
    dependency = Dependency()
    await fail_times(breaker, dependency, 2)
    dependency.mode = "ok"
    await breaker.call(dependency)
    dependency.mode = "fail"

    await fail_times(breaker, dependency, 2)

    assert state(breaker) is BreakerState.CLOSED


async def test_timeouts_count_as_failures(breaker: CircuitBreaker) -> None:
    dependency = Dependency()
    dependency.mode = "hang"

    for _ in range(3):
        with pytest.raises(TimeoutError):
            await breaker.call(dependency)

    assert state(breaker) is BreakerState.OPEN


async def test_half_open_trial_success_closes(breaker: CircuitBreaker, clock: FakeClock) -> None:
    dependency = Dependency()
    await fail_times(breaker, dependency, 3)
    clock.now += OPEN_SECONDS
    dependency.mode = "ok"

    assert await breaker.call(dependency) == "ok"

    assert state(breaker) is BreakerState.CLOSED
    assert await breaker.call(dependency) == "ok"


async def test_half_open_trial_failure_reopens_for_a_full_period(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    dependency = Dependency()
    await fail_times(breaker, dependency, 3)
    clock.now += OPEN_SECONDS

    await fail_times(breaker, dependency, 1)  # the single trial
    assert state(breaker) is BreakerState.OPEN

    clock.now += OPEN_SECONDS - 1
    with pytest.raises(CircuitOpenError):
        await breaker.call(dependency)
    assert dependency.calls == 4

    clock.now += 1
    dependency.mode = "ok"
    assert await breaker.call(dependency) == "ok"
    assert state(breaker) is BreakerState.CLOSED


async def test_only_one_trial_call_while_half_open(
    breaker: CircuitBreaker, clock: FakeClock
) -> None:
    dependency = Dependency()
    await fail_times(breaker, dependency, 3)
    clock.now += OPEN_SECONDS
    release = asyncio.Event()

    async def slow_success() -> str:
        await release.wait()
        return "ok"

    trial = asyncio.create_task(breaker.call(slow_success))
    await asyncio.sleep(0)  # let the trial start
    with pytest.raises(CircuitOpenError):
        await breaker.call(dependency)
    release.set()

    assert await trial == "ok"
    assert state(breaker) is BreakerState.CLOSED


async def test_non_dependency_errors_are_not_counted(breaker: CircuitBreaker) -> None:
    async def buggy() -> str:
        raise ValueError("bug in our code, not a Redis failure")

    for _ in range(5):
        with pytest.raises(ValueError):
            await breaker.call(buggy)

    assert state(breaker) is BreakerState.CLOSED


async def test_state_transitions_are_logged_once_not_per_call(
    breaker: CircuitBreaker, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.core.circuit_breaker")
    dependency = Dependency()

    await fail_times(breaker, dependency, 3)
    for _ in range(20):
        with pytest.raises(CircuitOpenError):
            await breaker.call(dependency)
    clock.now += OPEN_SECONDS
    dependency.mode = "ok"
    for _ in range(5):
        await breaker.call(dependency)

    transitions = [
        (r.from_state, r.to_state)  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "circuit breaker state change"
    ]
    assert transitions == [("closed", "open"), ("open", "half_open"), ("half_open", "closed")]
