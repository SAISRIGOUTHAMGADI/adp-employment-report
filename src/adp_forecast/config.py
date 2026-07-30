"""Configuration and the canonical series registry.

Two responsibilities, both read-only at runtime:

1. Load settings from the environment (``.env`` supported via python-dotenv).
2. Declare *which* series this project tracks and how each behaves.

The registry is the single source of truth for the series set. Nothing downstream
hardcodes a series ID; the ingestion, feature and explanation layers all iterate
the registry, so adding a series is a one-entry change here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final, Mapping

from dotenv import load_dotenv

from .domain import Frequency, SeriesRole, SeriesSpec
from .exceptions import ConfigurationError

# Read .env once at import. override=False so a real environment variable always
# beats the file, which is what CI and container deploys expect.
load_dotenv(override=False)

_API_KEY_ENV_VAR: Final[str] = "FRED_API_KEY"

#: FRED API keys are 32 lowercase alphanumeric characters. Validating the shape
#: locally turns a confusing HTTP 400 into an actionable startup error.
_API_KEY_LENGTH: Final[int] = 32

#: The target series. Named because several layers need to reference it directly
#: and a typo'd string literal would fail far from its cause.
TARGET_SERIES_ID: Final[str] = "ADPMNUSNERSA"

#: FRED release ID for the ADP National Employment Report. Needed to fetch real
#: historical release dates, which the backtest uses as forecast origins.
ADP_RELEASE_ID: Final[int] = 194

# ---------------------------------------------------------------------------
# Regime exclusion
# ---------------------------------------------------------------------------
# The pandemic window is excluded from model training. The boundary was chosen from
# the data, not assumed: measured against pre-2020 volatility (mean +171k, sd 75k),
# every month in this range exceeds 4 standard deviations, and 2022-07 onward is back
# within 3.2. The window covers three distinct phases -- the collapse (2020-03 to
# 2020-08, as low as -1,828k), a second dip (2021-01 to 2021-03), and the reopening
# boom (2021-07 to 2022-06, up to +947k).
#
# The rebound is excluded as deliberately as the collapse. Twelve consecutive months
# above 4 sd is a regime, and leaving it in would teach the model that +900k months are
# ordinary. Both phases are real economic history; neither is repeatable dynamics a
# 2026 forecast can learn from.
#
# Excluded rather than winsorised. Clipping these to a plausible bound would fabricate
# observations that never happened and quietly present them as data.

#: First month excluded from training (inclusive).
COVID_EXCLUSION_START: Final[date] = date(2020, 3, 1)

#: Last month excluded from training (inclusive).
COVID_EXCLUSION_END: Final[date] = date(2022, 6, 1)


def is_excluded_month(month: date) -> bool:
    """Whether a reference month falls in the excluded pandemic regime.

    Args:
        month: Reference month, as any date within it.
    """
    first = month.replace(day=1)
    return COVID_EXCLUSION_START <= first <= COVID_EXCLUSION_END


#: Raw ``Persons`` to thousands of persons. ADP publishes 132,722,000 where BLS
#: publishes 135,613 for a comparable magnitude.
_PERSONS_TO_THOUSANDS: Final[float] = 0.001


@dataclass(frozen=True, slots=True)
class FredSettings:
    """Transport settings for the FRED adapter.

    Defaults encode what we measured against the live API rather than guesses:
    FRED answers well under a second, returns HTTP 400 (never 5xx) for bad input,
    and throttles at roughly 120 requests/minute.

    Attributes:
        api_key: FRED API key.
        base_url: API root, without a trailing slash.
        timeout_seconds: Per-request timeout. Applies to connect and read.
        max_retries: Retry attempts *after* the initial try. 0 disables retrying.
        backoff_base_seconds: First backoff interval; doubles each attempt.
        backoff_max_seconds: Ceiling on any single backoff interval.
        user_agent: Sent on every request so upstream can attribute traffic.
    """

    api_key: str
    base_url: str = "https://api.stlouisfed.org/fred"
    timeout_seconds: float = 15.0
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    user_agent: str = "adp-forecast/0.1 (+https://github.com/SAISRIGOUTHAMGADI)"

    @classmethod
    def from_env(cls, **overrides: object) -> "FredSettings":
        """Build settings from the environment.

        Args:
            **overrides: Any field to override, for tests or tuning.

        Returns:
            A populated :class:`FredSettings`.

        Raises:
            ConfigurationError: If ``FRED_API_KEY`` is absent or empty.
        """
        return cls(api_key=require_api_key(), **overrides)  # type: ignore[arg-type]


def get_api_key() -> str | None:
    """Return the configured FRED API key, or ``None`` if unset.

    Non-raising counterpart to :func:`require_api_key`, used by tests to decide
    whether live integration tests can run.
    """
    key = os.getenv(_API_KEY_ENV_VAR, "").strip()
    return key or None


def require_api_key() -> str:
    """Return the configured FRED API key.

    Raises:
        ConfigurationError: If the key is missing, or is not the 32-character
            shape FRED issues.
    """
    key = get_api_key()
    if key is None:
        raise ConfigurationError(
            f"{_API_KEY_ENV_VAR} is not set. Copy .env.example to .env and add your "
            "free key from https://fredaccount.stlouisfed.org/apikeys"
        )
    if len(key) != _API_KEY_LENGTH or not key.isalnum():
        raise ConfigurationError(
            f"{_API_KEY_ENV_VAR} does not look like a FRED key: expected "
            f"{_API_KEY_LENGTH} alphanumeric characters, got {len(key)}."
        )
    return key


# ---------------------------------------------------------------------------
# Series registry
# ---------------------------------------------------------------------------
# Every fact here was verified against the live FRED API on 2026-07-30 rather
# than assumed. The two that most often go wrong are units (ADP is Persons, BLS
# is Thousands) and publication lag (JOLTS trails everything else by an extra
# month).

_SERIES: Final[tuple[SeriesSpec, ...]] = (
    SeriesSpec(
        series_id=TARGET_SERIES_ID,
        role=SeriesRole.TARGET,
        frequency=Frequency.MONTHLY,
        label="ADP private payrolls",
        units="Persons",
        scale_to_thousands=_PERSONS_TO_THOUSANDS,
        publication_lag_months=1,
        description=(
            "The forecast target. ADP's headline is the month-over-month change in "
            "this level. Revised only once a year, at the January QCEW rebenchmark."
        ),
    ),
    SeriesSpec(
        series_id="ICSA",
        role=SeriesRole.FEATURE,
        frequency=Frequency.WEEKLY,
        label="Initial jobless claims",
        units="Number",
        scale_to_thousands=_PERSONS_TO_THOUSANDS,
        publication_lag_months=0,
        description=(
            "Weekly count of new unemployment filings: the flow into unemployment. "
            "The most timely labour-market signal available and the first to turn."
        ),
    ),
    SeriesSpec(
        series_id="CCSA",
        role=SeriesRole.FEATURE,
        frequency=Frequency.WEEKLY,
        label="Continued jobless claims",
        units="Number",
        scale_to_thousands=_PERSONS_TO_THOUSANDS,
        publication_lag_months=0,
        description=(
            "Weekly count of people still drawing benefits: the stock of unemployment. "
            "Confirms whether a move in initial claims is a blip or a trend."
        ),
    ),
    SeriesSpec(
        series_id="USPRIV",
        role=SeriesRole.FEATURE,
        frequency=Frequency.MONTHLY,
        label="BLS private payrolls",
        units="Thousands of Persons",
        publication_lag_months=1,
        description=(
            "The BLS measure of the same concept ADP measures, and therefore the "
            "correct official comparator. Revised twice after first print."
        ),
    ),
    SeriesSpec(
        series_id="PAYEMS",
        role=SeriesRole.FEATURE,
        frequency=Frequency.MONTHLY,
        label="BLS total nonfarm payrolls",
        units="Thousands of Persons",
        publication_lag_months=1,
        description=(
            "Total nonfarm including government. Carried mainly so that "
            "PAYEMS - USPRIV yields government payrolls as a free derived feature."
        ),
    ),
    SeriesSpec(
        series_id="UNRATE",
        role=SeriesRole.FEATURE,
        frequency=Frequency.MONTHLY,
        label="Unemployment rate",
        units="Percent",
        publication_lag_months=1,
        description=(
            "Household-survey unemployment rate. Coincident rather than leading, so "
            "expected to carry less signal than claims; retained as a level check."
        ),
    ),
    SeriesSpec(
        series_id="JTSJOL",
        role=SeriesRole.FEATURE,
        frequency=Frequency.MONTHLY,
        label="Job openings (JOLTS)",
        units="Level in Thousands",
        publication_lag_months=2,
        description=(
            "Open positions: labour demand. Published a full month later than the "
            "other monthly series, so only month T-2 is available when forecasting T."
        ),
    ),
)

#: Read-only registry keyed by series ID. MappingProxyType prevents a downstream
#: module from mutating shared configuration. Lookup is O(1).
SERIES_REGISTRY: Final[Mapping[str, SeriesSpec]] = MappingProxyType(
    {spec.series_id: spec for spec in _SERIES}
)


def get_series_spec(series_id: str) -> SeriesSpec:
    """Return the registry entry for ``series_id``.

    Args:
        series_id: Upstream series identifier.

    Raises:
        ConfigurationError: If the series is not in the registry. Raised eagerly
            so a typo surfaces here rather than as an empty result set later.
    """
    try:
        return SERIES_REGISTRY[series_id]
    except KeyError:
        known = ", ".join(sorted(SERIES_REGISTRY))
        raise ConfigurationError(
            f"Unknown series '{series_id}'. Registered series: {known}"
        ) from None


def series_ids_for_role(role: SeriesRole) -> tuple[str, ...]:
    """Return registered series IDs filtered by role, in registry order.

    Args:
        role: The role to filter on.
    """
    return tuple(spec.series_id for spec in _SERIES if spec.role is role)


def all_series_ids() -> tuple[str, ...]:
    """Return every registered series ID, target first."""
    return tuple(spec.series_id for spec in _SERIES)
