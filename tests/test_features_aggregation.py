"""Unit tests for weekly-to-monthly aggregation."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, Observation
from adp_forecast.exceptions import VintageMismatchError
from adp_forecast.features import (
    AggregationMethod,
    aggregate_to_monthly,
    monthly_values_from_monthly_observations,
)
from adp_forecast.features.aggregation import _RULES

FETCHED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


def weekly(
    obs_date: date,
    value: float | None,
    series_id: str = "ICSA",
    realtime_start: date = date(2026, 1, 1),
) -> Observation:
    """A weekly observation dated by its week-ending Saturday."""
    return Observation(
        series_id=series_id,
        date=obs_date,
        value=value,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=realtime_start,
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    )


#: Five real week-ending Saturdays in July 2026, with plausible claim counts.
JULY_2026 = [
    weekly(date(2026, 7, 4), 200_000.0),
    weekly(date(2026, 7, 11), 210_000.0),
    weekly(date(2026, 7, 18), 190_000.0),
    weekly(date(2026, 7, 25), 200_000.0),
]


# -- the default rule ----------------------------------------------------------


def test_calendar_month_mean_averages_every_week():
    result = aggregate_to_monthly(JULY_2026)

    assert len(result) == 1
    assert result[0].month == date(2026, 7, 1)
    assert result[0].value == pytest.approx(200.0)  # thousands
    assert result[0].weeks_used == 4


def test_values_are_converted_to_canonical_units():
    """ICSA publishes raw counts; the panel must speak thousands like everything else."""
    result = aggregate_to_monthly([weekly(date(2026, 7, 4), 187_000.0)], min_weeks=1)

    assert result[0].value == pytest.approx(187.0)


def test_mean_not_sum_across_four_and_five_week_months():
    """A sum would inject a 25% swing from calendar drift alone."""
    four_week = aggregate_to_monthly(JULY_2026)
    five_week = aggregate_to_monthly(JULY_2026 + [weekly(date(2026, 7, 31), 200_000.0)])

    assert four_week[0].weeks_used == 4
    assert five_week[0].weeks_used == 5
    assert four_week[0].value == pytest.approx(five_week[0].value, rel=0.05)


def test_weeks_are_assigned_by_week_ending_date():
    """The week ending 2026-07-04 spans June and July but counts wholly as July."""
    result = aggregate_to_monthly(
        [
            weekly(date(2026, 6, 27), 100_000.0),
            weekly(date(2026, 7, 4), 300_000.0),
            weekly(date(2026, 7, 11), 300_000.0),
        ],
        min_weeks=1,
    )

    by_month = {value.month: value for value in result}
    assert by_month[date(2026, 6, 1)].value == pytest.approx(100.0)
    assert by_month[date(2026, 7, 1)].value == pytest.approx(300.0)


def test_months_are_returned_in_order():
    result = aggregate_to_monthly(
        [weekly(date(2026, 8, 1), 1.0), weekly(date(2026, 6, 27), 2.0)], min_weeks=1
    )

    assert [value.month for value in result] == [date(2026, 6, 1), date(2026, 8, 1)]


# -- partial months ------------------------------------------------------------


def test_month_below_minimum_weeks_is_missing_not_guessed():
    result = aggregate_to_monthly([weekly(date(2026, 7, 4), 200_000.0)], min_weeks=2)

    assert len(result) == 1, "the month is reported, not omitted"
    assert result[0].value is None
    assert result[0].is_missing
    assert result[0].weeks_used == 1


def test_month_meeting_the_minimum_is_kept():
    result = aggregate_to_monthly(JULY_2026[:2], min_weeks=2)

    assert result[0].value == pytest.approx(205.0)
    assert result[0].weeks_used == 2


def test_partial_current_month_alongside_complete_months():
    """The month in progress is usually partial at forecast time."""
    result = aggregate_to_monthly(
        [
            weekly(date(2026, 6, 6), 100_000.0),
            weekly(date(2026, 6, 13), 100_000.0),
            weekly(date(2026, 6, 20), 100_000.0),
            weekly(date(2026, 6, 27), 100_000.0),
            weekly(date(2026, 7, 4), 200_000.0),
        ],
        min_weeks=2,
    )

    by_month = {value.month: value for value in result}
    assert by_month[date(2026, 6, 1)].value == pytest.approx(100.0)
    assert by_month[date(2026, 7, 1)].is_missing


@pytest.mark.parametrize("min_weeks", [0, -1])
def test_invalid_minimum_is_rejected(min_weeks):
    with pytest.raises(ValueError, match="min_weeks"):
        aggregate_to_monthly(JULY_2026, min_weeks=min_weeks)


# -- missing values ------------------------------------------------------------


def test_missing_weeks_are_excluded_from_the_mean():
    """One '.' week must not poison the month; the others still average."""
    result = aggregate_to_monthly(
        [
            weekly(date(2026, 7, 4), 200_000.0),
            weekly(date(2026, 7, 11), None),
            weekly(date(2026, 7, 18), 200_000.0),
        ],
        min_weeks=2,
    )

    assert result[0].value == pytest.approx(200.0)
    assert result[0].weeks_used == 2, "the missing week does not count toward the minimum"


def test_month_of_entirely_missing_weeks_is_reported_missing():
    result = aggregate_to_monthly(
        [weekly(date(2026, 7, 4), None), weekly(date(2026, 7, 11), None)], min_weeks=1
    )

    assert len(result) == 1
    assert result[0].is_missing
    assert result[0].weeks_used == 0


def test_empty_input_yields_nothing():
    assert aggregate_to_monthly([]) == []


# -- vintage and series guards -------------------------------------------------


def test_mixed_series_are_rejected():
    with pytest.raises(VintageMismatchError, match="multiple series"):
        aggregate_to_monthly(
            [
                weekly(date(2026, 7, 4), 1.0, series_id="ICSA"),
                weekly(date(2026, 7, 11), 1.0, series_id="CCSA"),
            ]
        )


def test_duplicate_reference_date_is_rejected_as_multi_vintage():
    """Unfiltered vintage history would average two bases together."""
    with pytest.raises(VintageMismatchError, match="more than once"):
        aggregate_to_monthly(
            [
                weekly(date(2026, 7, 4), 200_000.0, realtime_start=date(2026, 7, 9)),
                weekly(date(2026, 7, 4), 201_000.0, realtime_start=date(2026, 7, 16)),
            ]
        )


def test_as_of_defaults_to_the_snapshot_date():
    result = aggregate_to_monthly(JULY_2026, min_weeks=1)

    assert all(value.as_of == date(2026, 1, 1) for value in result)


def test_explicit_as_of_is_stamped_through():
    result = aggregate_to_monthly(JULY_2026, as_of=date(2026, 6, 30), min_weeks=1)

    assert all(value.as_of == date(2026, 6, 30) for value in result)


# -- the swappable seam --------------------------------------------------------


def test_default_method_is_the_calendar_month_mean():
    explicit = aggregate_to_monthly(
        JULY_2026, method=AggregationMethod.CALENDAR_MONTH_MEAN
    )

    assert aggregate_to_monthly(JULY_2026) == explicit


def test_every_enum_member_has_a_registered_rule():
    """The seam must not offer a method that dispatches to nothing."""
    assert set(_RULES) == set(AggregationMethod)


def test_unregistered_method_raises_rather_than_silently_defaulting():
    class Bogus(str):
        pass

    with pytest.raises(ValueError, match="No aggregation rule"):
        aggregate_to_monthly(JULY_2026, method=Bogus("nope"))  # type: ignore[arg-type]


# -- monthly passthrough -------------------------------------------------------


def test_monthly_observations_map_straight_across():
    result = monthly_values_from_monthly_observations(
        [
            weekly(date(2026, 5, 1), 135_428.0, series_id="USPRIV"),
            weekly(date(2026, 6, 1), 135_500.0, series_id="USPRIV"),
        ]
    )

    assert [value.month for value in result] == [date(2026, 5, 1), date(2026, 6, 1)]
    assert result[0].value == pytest.approx(135_428.0)
    assert result[0].weeks_used == 1


def test_monthly_passthrough_marks_missing_values():
    result = monthly_values_from_monthly_observations(
        [weekly(date(2026, 5, 1), None, series_id="UNRATE")]
    )

    assert result[0].is_missing
    assert result[0].weeks_used == 0


def test_monthly_passthrough_rejects_mixed_vintages():
    with pytest.raises(VintageMismatchError):
        monthly_values_from_monthly_observations(
            [
                weekly(date(2026, 5, 1), 1.0, series_id="USPRIV",
                       realtime_start=date(2026, 6, 5)),
                weekly(date(2026, 5, 1), 2.0, series_id="USPRIV",
                       realtime_start=date(2026, 7, 2)),
            ]
        )
