"""Reusable retry policy.

Source-agnostic on purpose: it retries on our own
:class:`~adp_forecast.exceptions.TransientIngestionError` rather than on HTTP status
codes, so a future database or S3 adapter reuses this unchanged by classifying its
own failures into the transient/permanent split.

Policy is exponential backoff with full jitter. Jitter matters because the ingest
loop issues one request per series back to back; synchronised retries from a
correlated failure would arrive as a thundering herd against a rate-limited API.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Final, TypeVar

from .exceptions import TransientIngestionError
from .logging_config import get_logger

_LOG = get_logger(__name__)

T = TypeVar("T")

#: Sleep function, module-level so tests can patch it and run instantly.
_sleep: Final[Callable[[float], None]] = time.sleep


def call_with_retry(
    operation: Callable[[], T],
    *,
    max_retries: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
    description: str,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """Invoke ``operation``, retrying transient failures with jittered backoff.

    Permanent failures propagate on the first attempt. That is the point of the
    exception split: FRED answers a bad series ID or a bad key with HTTP 400, so
    retrying a typo would burn three slots of a ~120 req/min budget and delay the
    real error by seconds for no possible gain.

    Args:
        operation: Zero-argument callable performing one attempt.
        max_retries: Attempts *after* the first. ``0`` means try once.
        backoff_base_seconds: First backoff interval; doubles per attempt.
        backoff_max_seconds: Upper bound on any single sleep.
        description: Human-readable operation name, used in log messages.
        sleep: Override for the sleep function (tests).
        rng: Override for the jitter source (tests, for determinism).

    Returns:
        Whatever ``operation`` returns on its first success.

    Raises:
        TransientIngestionError: Re-raised from the final failed attempt once
            retries are exhausted.
        PermanentIngestionError: Propagated immediately, never retried.
    """
    sleeper = sleep if sleep is not None else _sleep
    jitter = rng if rng is not None else random
    total_attempts = max_retries + 1
    last_error: TransientIngestionError | None = None

    for attempt in range(1, total_attempts + 1):
        try:
            return operation()
        except TransientIngestionError as exc:
            last_error = exc
            if attempt == total_attempts:
                break
            delay = _backoff_delay(
                attempt=attempt,
                base=backoff_base_seconds,
                ceiling=backoff_max_seconds,
                jitter=jitter,
            )
            _LOG.warning(
                "%s failed (attempt %d/%d): %s. Retrying in %.2fs",
                description,
                attempt,
                total_attempts,
                exc,
                delay,
            )
            sleeper(delay)

    _LOG.error("%s failed after %d attempts: %s", description, total_attempts, last_error)
    assert last_error is not None  # loop only breaks after assigning last_error
    raise last_error


def _backoff_delay(
    *,
    attempt: int,
    base: float,
    ceiling: float,
    jitter: random.Random,
) -> float:
    """Return a full-jitter backoff delay for a 1-indexed attempt number.

    Full jitter (uniform over ``[0, capped]``) rather than equal jitter, because it
    minimises contention when several callers back off simultaneously.
    """
    capped = min(base * (2 ** (attempt - 1)), ceiling)
    return jitter.uniform(0.0, capped)
