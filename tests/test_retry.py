"""Unit tests for the reusable retry policy."""

from __future__ import annotations

import random

import pytest

from adp_forecast.exceptions import PermanentIngestionError, TransientIngestionError
from adp_forecast.retry import _backoff_delay, call_with_retry

RETRY_KWARGS = {
    "max_retries": 3,
    "backoff_base_seconds": 1.0,
    "backoff_max_seconds": 8.0,
    "description": "test operation",
}


class Counter:
    """Callable that fails a fixed number of times before succeeding."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        self.failures = failures
        self.calls = 0
        self._error = error or TransientIngestionError("boom")

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self._error
        return "ok"


def test_succeeds_without_retry():
    operation = Counter(failures=0)

    assert call_with_retry(operation, sleep=lambda _: None, **RETRY_KWARGS) == "ok"
    assert operation.calls == 1


def test_retries_until_success():
    operation = Counter(failures=2)

    assert call_with_retry(operation, sleep=lambda _: None, **RETRY_KWARGS) == "ok"
    assert operation.calls == 3


def test_raises_after_exhausting_retries():
    operation = Counter(failures=99)

    with pytest.raises(TransientIngestionError):
        call_with_retry(operation, sleep=lambda _: None, **RETRY_KWARGS)

    assert operation.calls == 4, "initial attempt plus max_retries"


def test_permanent_error_is_not_retried():
    operation = Counter(failures=99, error=PermanentIngestionError("typo"))

    with pytest.raises(PermanentIngestionError):
        call_with_retry(operation, sleep=lambda _: None, **RETRY_KWARGS)

    assert operation.calls == 1


def test_zero_retries_attempts_once():
    operation = Counter(failures=99)
    kwargs = {**RETRY_KWARGS, "max_retries": 0}

    with pytest.raises(TransientIngestionError):
        call_with_retry(operation, sleep=lambda _: None, **kwargs)

    assert operation.calls == 1


def test_sleep_is_called_between_attempts():
    delays: list[float] = []
    operation = Counter(failures=2)

    call_with_retry(operation, sleep=delays.append, **RETRY_KWARGS)

    assert len(delays) == 2, "one sleep per retry, none after the final success"


def test_backoff_is_bounded_and_non_negative():
    """Full jitter must stay within [0, min(base * 2^(n-1), ceiling)]."""
    rng = random.Random(0)
    for attempt in range(1, 8):
        delay = _backoff_delay(attempt=attempt, base=1.0, ceiling=8.0, jitter=rng)
        assert 0.0 <= delay <= 8.0


def test_backoff_ceiling_caps_growth():
    """Always-max jitter shows the exponential curve flattening at the ceiling."""

    class MaxJitter(random.Random):
        def uniform(self, a: float, b: float) -> float:  # noqa: D102
            return b

    rng = MaxJitter()
    delays = [
        _backoff_delay(attempt=n, base=1.0, ceiling=8.0, jitter=rng)
        for n in range(1, 7)
    ]

    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]
