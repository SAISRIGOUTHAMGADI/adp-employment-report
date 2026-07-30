"""Unit tests for vintage-safe month-over-month differencing.

The cross-vintage tests use the real ADP rebenchmark figures. Those numbers are the
whole reason this guard exists: differencing across the January restatement fabricates
a -2,307k print against a true +22k, with no error raised anywhere unless something
like this refuses it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.config import TARGET_SERIES_ID
from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, MonthlyValue, Observation
from adp_forecast.exceptions import VintageMismatchError
from adp_forecast.features import (
    change_series,
    month_over_month_change,
    monthly_value_changes,
)

FETCHED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


def adp(
    obs_date: date,
    value: float | None,
    realtime_start: date = date(2026, 3, 4),
    realtime_end: date = CURRENT_VINTAGE_SENTINEL,
) -> Observation:
    """An ADP observation, in raw Persons as FRED publishes it."""
    return Observation(
        series_id=TARGET_SERIES_ID,
        date=obs_date,
        value=value,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )


AS_OF = date(2026, 3, 10)


# -- the happy path ------------------------------------------------------------


def test_change_is_computed_in_canonical_units():
    """132,624,000 -> 132,722,000 Persons is the +98k headline."""
    change = month_over_month_change(
        adp(date(2026, 6, 1), 132_722_000.0),
        adp(date(2026, 5, 1), 132_624_000.0),
        as_of=AS_OF,
    )

    assert change is not None
    assert change.change == pytest.approx(98.0)
    assert change.level == pytest.approx(132_722.0)
    assert change.previous_level == pytest.approx(132_624.0)
    assert change.month == date(2026, 6, 1)
    assert change.as_of == AS_OF


def test_change_across_a_year_boundary_is_allowed():
    """December to January is consecutive; only *cross-vintage* pairs are refused."""
    change = month_over_month_change(
        adp(date(2026, 1, 1), 132_270_000.0),
        adp(date(2025, 12, 1), 132_259_000.0),
        as_of=AS_OF,
    )

    assert change is not None
    assert change.change == pytest.approx(11.0)


def test_missing_level_yields_none_rather_than_raising():
    assert (
        month_over_month_change(
            adp(date(2026, 6, 1), None),
            adp(date(2026, 5, 1), 132_624_000.0),
            as_of=AS_OF,
        )
        is None
    )


# -- the cross-vintage guard ---------------------------------------------------


def test_cross_vintage_subtraction_is_refused():
    """The real 2026 rebenchmark: -2,307k fabricated against a true +22k."""
    pre_rebenchmark = adp(
        date(2025, 12, 1),
        134_588_000.0,
        realtime_start=date(2026, 1, 7),
        realtime_end=date(2026, 2, 3),
    )
    post_rebenchmark = adp(
        date(2026, 1, 1),
        132_281_000.0,
        realtime_start=date(2026, 2, 4),
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    )

    with pytest.raises(VintageMismatchError, match="different benchmark bases"):
        month_over_month_change(
            post_rebenchmark, pre_rebenchmark, as_of=date(2026, 2, 10)
        )


def test_the_refused_subtraction_would_have_been_catastrophically_wrong():
    """Documents the magnitude the guard prevents, using the real January 2026 figures."""
    fabricated = (132_281_000.0 - 134_588_000.0) / 1000.0
    published = (132_281_000.0 - 132_259_000.0) / 1000.0

    assert fabricated == pytest.approx(-2_307.0)
    assert published == pytest.approx(22.0)
    assert fabricated < 0 < published, "the fabricated print even flips the sign"
    assert abs(fabricated / published) > 100, "two orders of magnitude off"


def test_operand_not_published_on_as_of_is_refused():
    """An operand superseded before the vantage date is not part of that snapshot."""
    superseded = adp(
        date(2026, 5, 1),
        132_624_000.0,
        realtime_start=date(2026, 6, 3),
        realtime_end=date(2026, 7, 1),
    )
    current = adp(
        date(2026, 6, 1), 132_722_000.0, realtime_start=date(2026, 6, 3)
    )

    with pytest.raises(VintageMismatchError, match="not the published value"):
        month_over_month_change(current, superseded, as_of=date(2026, 7, 20))


def test_non_consecutive_months_are_refused():
    with pytest.raises(VintageMismatchError, match="consecutive months"):
        month_over_month_change(
            adp(date(2026, 6, 1), 1.0), adp(date(2026, 4, 1), 1.0), as_of=AS_OF
        )


def test_reversed_order_is_refused():
    with pytest.raises(VintageMismatchError, match="consecutive months"):
        month_over_month_change(
            adp(date(2026, 5, 1), 1.0), adp(date(2026, 6, 1), 1.0), as_of=AS_OF
        )


def test_cross_series_subtraction_is_refused():
    other = Observation(
        series_id="USPRIV",
        date=date(2026, 5, 1),
        value=135_428.0,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=date(2026, 3, 4),
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    )

    with pytest.raises(VintageMismatchError, match="across series"):
        month_over_month_change(adp(date(2026, 6, 1), 1.0), other, as_of=AS_OF)


def test_as_of_is_a_required_keyword():
    """The vantage point must be stated, never inferred."""
    with pytest.raises(TypeError):
        month_over_month_change(  # type: ignore[call-arg]
            adp(date(2026, 6, 1), 1.0), adp(date(2026, 5, 1), 1.0)
        )


# -- series-level differencing -------------------------------------------------


def test_change_series_produces_one_change_per_consecutive_pair():
    observations = [
        adp(date(2026, 3, 1), 132_397_000.0),
        adp(date(2026, 4, 1), 132_502_000.0),
        adp(date(2026, 5, 1), 132_624_000.0),
        adp(date(2026, 6, 1), 132_722_000.0),
    ]

    changes = change_series(observations, as_of=AS_OF)

    assert [round(change.change) for change in changes] == [105, 122, 98]
    assert [change.month for change in changes] == [
        date(2026, 4, 1),
        date(2026, 5, 1),
        date(2026, 6, 1),
    ]


def test_change_series_sorts_before_differencing():
    observations = [
        adp(date(2026, 6, 1), 132_722_000.0),
        adp(date(2026, 4, 1), 132_502_000.0),
        adp(date(2026, 5, 1), 132_624_000.0),
    ]

    changes = change_series(observations, as_of=AS_OF)

    assert [round(change.change) for change in changes] == [122, 98]


def test_change_series_skips_a_gap_rather_than_bridging_it():
    """A change across a hole is not a month-over-month change."""
    observations = [
        adp(date(2026, 1, 1), 132_270_000.0),
        adp(date(2026, 4, 1), 132_502_000.0),
        adp(date(2026, 5, 1), 132_624_000.0),
    ]

    changes = change_series(observations, as_of=AS_OF)

    assert [change.month for change in changes] == [date(2026, 5, 1)]


def test_change_series_omits_pairs_with_a_missing_level():
    observations = [
        adp(date(2026, 4, 1), 132_502_000.0),
        adp(date(2026, 5, 1), None),
        adp(date(2026, 6, 1), 132_722_000.0),
    ]

    assert change_series(observations, as_of=AS_OF) == []


@pytest.mark.parametrize("count", [0, 1])
def test_change_series_needs_two_observations(count):
    observations = [adp(date(2026, 6, 1), 1.0)][:count]

    assert change_series(observations, as_of=AS_OF) == []


# -- monthly-value differencing ------------------------------------------------


def monthly(month: date, value: float | None, as_of: date = AS_OF) -> MonthlyValue:
    """An already-aggregated monthly value in canonical units."""
    return MonthlyValue(
        series_id="ICSA", month=month, value=value, weeks_used=4, as_of=as_of
    )


def test_monthly_value_changes_difference_without_reconverting():
    """Aggregated values are already canonical; converting again would divide twice."""
    changes = monthly_value_changes(
        [monthly(date(2026, 5, 1), 200.0), monthly(date(2026, 6, 1), 190.0)]
    )

    assert len(changes) == 1
    assert changes[0].change == pytest.approx(-10.0)
    assert changes[0].level == pytest.approx(190.0)


def test_monthly_value_changes_skip_missing_sides():
    changes = monthly_value_changes(
        [
            monthly(date(2026, 4, 1), 200.0),
            monthly(date(2026, 5, 1), None),
            monthly(date(2026, 6, 1), 190.0),
        ]
    )

    assert changes == []


def test_monthly_value_changes_skip_month_gaps():
    changes = monthly_value_changes(
        [monthly(date(2026, 1, 1), 200.0), monthly(date(2026, 6, 1), 190.0)]
    )

    assert changes == []


def test_monthly_values_from_different_snapshots_are_refused():
    """Differing as_of means differing snapshots, which is the same bug by another route."""
    with pytest.raises(VintageMismatchError, match="multiple snapshots"):
        monthly_value_changes(
            [
                monthly(date(2026, 5, 1), 200.0, as_of=date(2026, 6, 1)),
                monthly(date(2026, 6, 1), 190.0, as_of=date(2026, 7, 1)),
            ]
        )


def test_monthly_value_changes_reject_mixed_series():
    values = [
        monthly(date(2026, 5, 1), 200.0),
        MonthlyValue("CCSA", date(2026, 6, 1), 190.0, 4, AS_OF),
    ]

    with pytest.raises(VintageMismatchError, match="across series"):
        monthly_value_changes(values)
