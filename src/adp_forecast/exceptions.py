"""Exception hierarchy for the adp_forecast package.

Every error raised by this package derives from :class:`AdpForecastError`, so callers
can catch the whole package with one handler or narrow to a specific failure mode.

The split that matters most is :class:`TransientIngestionError` versus
:class:`PermanentIngestionError`. Retry logic keys off those two types rather than
off HTTP status codes, which keeps the retry policy independent of any one adapter's
transport. A FRED typo and a Postgres syntax error are both permanent; a 503 and a
socket timeout are both transient.
"""

from __future__ import annotations


class AdpForecastError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(AdpForecastError):
    """Required configuration is missing or malformed (e.g. absent API key)."""


class IngestionError(AdpForecastError):
    """Base class for failures originating in the ingestion layer."""


class TransientIngestionError(IngestionError):
    """A failure that may succeed if retried (timeout, connection reset, 5xx).

    Raised only for conditions where a retry is plausibly useful. Callers and retry
    decorators treat this as "back off and try again".
    """


class PermanentIngestionError(IngestionError):
    """A failure that will recur identically if retried (bad series ID, bad key).

    Retrying these wastes the API rate limit and delays surfacing the real bug, so
    the retry policy re-raises them immediately.
    """


class SeriesNotFoundError(PermanentIngestionError):
    """The requested series ID does not exist at the upstream source."""


class AuthenticationError(PermanentIngestionError):
    """The upstream source rejected the supplied credentials."""


class RateLimitError(TransientIngestionError):
    """The upstream source is throttling us; retry after a backoff."""


class ResponseValidationError(PermanentIngestionError):
    """The upstream response was well-formed HTTP but not the expected payload shape.

    Treated as permanent: a schema mismatch means our parsing assumptions are wrong,
    and hammering the endpoint will not correct that.
    """


# class FeatureError(AdpForecastError):
#     """Base class for failures originating in the feature layer."""


class VintageMismatchError(AdpForecastError):
    """An arithmetic operation was attempted across two incompatible vintages.

    The structural guard against the highest-impact bug in this project. A rebenchmark
    restates the entire history of a series at once, so any single snapshot is
    internally consistent — but subtracting a pre-rebenchmark level from a
    post-rebenchmark one yields a change that was never published. For ADP that
    fabricates a -2,307k January 2026 print against a true +22k.

    Raised rather than silently corrected, on the same principle as the units choke
    point: make the mistake impossible to reintroduce quietly rather than relying on a
    convention, a comment, or a flag nobody will enable.
    """


class InsufficientDataError(AdpForecastError):
    """Not enough underlying observations existed to form a requested feature."""


# class StorageError(AdpForecastError):
#     """Base class for failures originating in the storage layer."""


class StorageIntegrityError(AdpForecastError):
    """The database rejected a write, or its contents violate an invariant."""


class VintageValidationError(AdpForecastError):
    """A batch of observations does not carry usable vintage windows.

    Guards the one write path against persisting *display-only* records. A
    current-vintage fetch reports every row's ``realtime_start`` as the fetch date
    rather than the real publication date, so writing those would look identical to
    genuine vintage data while silently destroying the backtest's point-in-time
    guarantee. Since both cases share ``realtime_end == CURRENT_VINTAGE_SENTINEL``,
    the schema cannot express the difference and this check has to.
    """
