"""Unit tests for the domain model, focused on the vintage predicate."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.domain import (
    CURRENT_VINTAGE_SENTINEL,
    Frequency,
    Observation,
    SeriesRole,
    SeriesSpec,
)

FETCHED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def make_observation(
    value: float | None = 135_428.0,
    realtime_start: date = date(2026, 5, 8),
    realtime_end: date = date(2026, 6, 4),
) -> Observation:
    """Build an observation with sensible defaults for the field under test."""
    return Observation(
        series_id="USPRIV",
        date=date(2026, 4, 1),
        value=value,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )


def test_is_missing_reflects_none_value():
    assert make_observation(value=None).is_missing
    assert not make_observation(value=0.0).is_missing, "zero is data, not absence"


def test_is_current_vintage_only_for_open_window():
    assert make_observation(realtime_end=CURRENT_VINTAGE_SENTINEL).is_current_vintage
    assert not make_observation(realtime_end=date(2026, 6, 4)).is_current_vintage


@pytest.mark.parametrize(
    "as_of, expected",
    [
        (date(2026, 5, 7), False),   # day before publication
        (date(2026, 5, 8), True),    # first day published: inclusive lower bound
        (date(2026, 5, 20), True),   # mid-window
        (date(2026, 6, 4), True),    # last day published: inclusive upper bound
        (date(2026, 6, 5), False),   # day after revision superseded it
    ],
)
def test_known_on_window_is_inclusive_at_both_ends(as_of, expected):
    """Off-by-one here silently leaks or drops a day of data in every backtest."""
    assert make_observation().known_on(as_of) is expected


def test_known_on_open_window_extends_indefinitely():
    obs = make_observation(realtime_end=CURRENT_VINTAGE_SENTINEL)

    assert obs.known_on(date(2026, 7, 30))
    assert obs.known_on(date(2099, 1, 1))


def test_observation_is_immutable():
    obs = make_observation()

    with pytest.raises((AttributeError, TypeError)):
        obs.value = 1.0  # type: ignore[misc]


def test_observation_is_hashable_for_set_deduplication():
    assert len({make_observation(), make_observation()}) == 1


def test_series_spec_is_weekly():
    weekly = SeriesSpec(
        series_id="ICSA",
        role=SeriesRole.FEATURE,
        frequency=Frequency.WEEKLY,
        label="Initial claims",
        units="Number",
    )
    monthly = SeriesSpec(
        series_id="UNRATE",
        role=SeriesRole.FEATURE,
        frequency=Frequency.MONTHLY,
        label="Unemployment rate",
        units="Percent",
    )

    assert weekly.is_weekly
    assert not monthly.is_weekly
