"""Unit tests for point-in-time feature panel assembly.

Runs against a real in-memory :class:`SqliteStorage`, so these exercise the storage
read path and the feature layer together — which is where a leak would actually appear.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.config import TARGET_SERIES_ID
from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, Observation
from adp_forecast.exceptions import InsufficientDataError
from adp_forecast.features import RELEASE_ORIGIN_OFFSET, FeaturePanelBuilder
from adp_forecast.storage import SqliteStorage

FETCHED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


def observation(
    series_id: str,
    obs_date: date,
    value: float | None,
    realtime_start: date,
    realtime_end: date = CURRENT_VINTAGE_SENTINEL,
) -> Observation:
    return Observation(
        series_id=series_id,
        date=obs_date,
        value=value,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )


#: ADP levels published on the first Wednesday following each reference month.
ADP_HISTORY = [
    observation(TARGET_SERIES_ID, date(2026, 2, 1), 132_336_000.0, date(2026, 3, 4)),
    observation(TARGET_SERIES_ID, date(2026, 3, 1), 132_397_000.0, date(2026, 4, 1)),
    observation(TARGET_SERIES_ID, date(2026, 4, 1), 132_502_000.0, date(2026, 5, 6)),
    observation(TARGET_SERIES_ID, date(2026, 5, 1), 132_624_000.0, date(2026, 6, 3)),
    observation(TARGET_SERIES_ID, date(2026, 6, 1), 132_722_000.0, date(2026, 7, 1)),
]

#: Weekly claims, published the Thursday after each week ends.
CLAIMS_HISTORY = [
    observation("ICSA", date(2026, 5, 2), 200_000.0, date(2026, 5, 7)),
    observation("ICSA", date(2026, 5, 9), 210_000.0, date(2026, 5, 14)),
    observation("ICSA", date(2026, 5, 16), 190_000.0, date(2026, 5, 21)),
    observation("ICSA", date(2026, 5, 23), 200_000.0, date(2026, 5, 28)),
    observation("ICSA", date(2026, 6, 6), 180_000.0, date(2026, 6, 11)),
    observation("ICSA", date(2026, 6, 13), 190_000.0, date(2026, 6, 18)),
    observation("ICSA", date(2026, 6, 20), 185_000.0, date(2026, 6, 25)),
    observation("ICSA", date(2026, 6, 27), 185_000.0, date(2026, 7, 2)),
]

#: JOLTS, which trails the other monthly series by an extra month.
JOLTS_HISTORY = [
    observation("JTSJOL", date(2026, 3, 1), 7_600.0, date(2026, 5, 5)),
    observation("JTSJOL", date(2026, 4, 1), 7_594.0, date(2026, 6, 2)),
]


@pytest.fixture
def store():
    with SqliteStorage(":memory:") as instance:
        instance.initialise()
        instance.upsert_observations(ADP_HISTORY + CLAIMS_HISTORY + JOLTS_HISTORY)
        yield instance


@pytest.fixture
def builder(store):
    return FeaturePanelBuilder(store)


# -- the one-day rule ----------------------------------------------------------


def test_build_for_release_reads_the_day_before(builder):
    """Reading on the release date itself would hand the model its own answer."""
    panel = builder.build_for_release(date(2026, 7, 1))

    assert panel.as_of == date(2026, 6, 30)
    assert RELEASE_ORIGIN_OFFSET.days == 1


def test_release_date_snapshot_excludes_the_month_being_released(builder):
    """June was published 2026-07-01, so it must be invisible at the 2026-06-30 origin."""
    panel = builder.build_for_release(date(2026, 7, 1))

    assert panel.target_month == date(2026, 6, 1), "June is what we forecast"
    assert panel.latest_target_month == date(2026, 5, 1), "May is the newest known"
    assert all(change.month < date(2026, 6, 1) for change in panel.target_changes)


def test_target_month_is_the_month_after_the_newest_known(builder):
    panel = builder.build(date(2026, 5, 20))

    assert panel.latest_target_month == date(2026, 4, 1)
    assert panel.target_month == date(2026, 5, 1)


def test_earlier_origins_see_strictly_less(builder):
    early = builder.build(date(2026, 4, 20))
    late = builder.build(date(2026, 6, 30))

    assert len(early.target_changes) < len(late.target_changes)
    assert early.target_month < late.target_month


# -- target changes ------------------------------------------------------------


def test_target_changes_are_the_published_headlines(builder):
    panel = builder.build(date(2026, 6, 30))

    assert [round(change.change) for change in panel.target_changes] == [61, 105, 122]
    assert [change.month for change in panel.target_changes] == [
        date(2026, 3, 1),
        date(2026, 4, 1),
        date(2026, 5, 1),
    ]


def test_target_changes_carry_the_panel_vantage(builder):
    panel = builder.build(date(2026, 6, 30))

    assert all(change.as_of == panel.as_of for change in panel.target_changes)


def test_insufficient_target_history_raises(builder):
    with pytest.raises(InsufficientDataError, match="at least 2"):
        builder.build(date(2026, 3, 10))


def test_origin_before_any_data_raises(builder):
    with pytest.raises(InsufficientDataError):
        builder.build(date(2020, 1, 1))


# -- feature normalisation -----------------------------------------------------


def test_weekly_features_are_aggregated_to_monthly(builder):
    panel = builder.build(date(2026, 6, 30))

    may = panel.feature_value_at("ICSA", date(2026, 5, 1))
    assert may == pytest.approx(200.0), "mean of 200/210/190/200 in thousands"


def test_monthly_features_pass_through(builder):
    panel = builder.build(date(2026, 6, 30))

    assert panel.feature_value_at("JTSJOL", date(2026, 4, 1)) == pytest.approx(7_594.0)


def test_target_is_not_duplicated_into_the_features(builder):
    panel = builder.build(date(2026, 6, 30))

    assert TARGET_SERIES_ID not in panel.feature_values


def test_feature_changes_are_derived_per_series(builder):
    panel = builder.build(date(2026, 6, 30))

    icsa = panel.feature_changes["ICSA"]
    assert len(icsa) == 1
    assert icsa[0].month == date(2026, 6, 1)
    assert icsa[0].change == pytest.approx(185.0 - 200.0)


def test_publication_lag_needs_no_manual_shift(builder):
    """JOLTS trails by a month; the snapshot reflects that without any lag arithmetic."""
    panel = builder.build(date(2026, 6, 30))

    assert panel.feature_value_at("JTSJOL", date(2026, 4, 1)) is not None
    assert panel.feature_value_at("JTSJOL", date(2026, 5, 1)) is None


def test_early_origin_sees_only_what_was_published_by_then(builder):
    """JOLTS March published 2026-05-05 is visible; April published 2026-06-02 is not."""
    panel = builder.build(date(2026, 5, 20))

    assert [value.month for value in panel.feature_values["JTSJOL"]] == [date(2026, 3, 1)]


def test_series_with_no_data_at_an_origin_is_empty_not_fabricated(builder):
    panel = builder.build(date(2026, 5, 20))

    assert panel.feature_values["PAYEMS"] == ()
    assert panel.feature_changes["PAYEMS"] == ()


def test_partial_month_is_missing_not_guessed(store):
    """A month with one week so far must not become a one-week estimate."""
    store.upsert_observations(
        [observation("ICSA", date(2026, 7, 4), 300_000.0, date(2026, 7, 9))]
    )
    panel = FeaturePanelBuilder(store).build(date(2026, 7, 15))

    assert panel.feature_value_at("ICSA", date(2026, 7, 1)) is None


def test_min_weeks_is_configurable(store):
    store.upsert_observations(
        [observation("ICSA", date(2026, 7, 4), 300_000.0, date(2026, 7, 9))]
    )
    panel = FeaturePanelBuilder(store, min_weeks=1).build(date(2026, 7, 15))

    assert panel.feature_value_at("ICSA", date(2026, 7, 1)) == pytest.approx(300.0)


# -- panel shape ---------------------------------------------------------------


def test_every_value_shares_the_panel_vantage(builder):
    """One snapshot per panel: a differing as_of anywhere would mean mixed vintages."""
    panel = builder.build(date(2026, 6, 30))

    vantages = {
        value.as_of
        for values in panel.feature_values.values()
        for value in values
    }
    assert vantages <= {panel.as_of}


def test_series_selection_is_honoured(builder):
    panel = builder.build(date(2026, 6, 30), series_ids=[TARGET_SERIES_ID, "ICSA"])

    assert set(panel.feature_values) == {"ICSA"}


def test_panel_mappings_are_immutable(builder):
    panel = builder.build(date(2026, 6, 30))

    with pytest.raises(TypeError):
        panel.feature_values["ICSA"] = ()  # type: ignore[index]


def test_months_available_counts_usable_values(builder):
    panel = builder.build(date(2026, 6, 30))

    assert panel.months_available(TARGET_SERIES_ID) == len(panel.target_changes)
    assert panel.months_available("ICSA") == 2
    assert panel.months_available("PAYEMS") == 0


def test_feature_value_at_accepts_any_day_in_the_month(builder):
    panel = builder.build(date(2026, 6, 30))

    assert panel.feature_value_at("ICSA", date(2026, 5, 17)) == pytest.approx(200.0)


def test_unknown_series_lookup_returns_none(builder):
    panel = builder.build(date(2026, 6, 30))

    assert panel.feature_value_at("NOPE", date(2026, 5, 1)) is None


# -- the leak test -------------------------------------------------------------


def test_a_revision_published_later_is_invisible_at_the_earlier_origin(store):
    """The point of the whole vintage design, exercised end to end."""
    store.upsert_observations(
        [
            observation(
                TARGET_SERIES_ID,
                date(2026, 5, 1),
                132_624_000.0,
                realtime_start=date(2026, 6, 3),
                realtime_end=date(2026, 7, 20),
            ),
            observation(
                TARGET_SERIES_ID,
                date(2026, 5, 1),
                132_900_000.0,
                realtime_start=date(2026, 7, 21),
            ),
        ]
    )
    builder = FeaturePanelBuilder(store)

    before = builder.build(date(2026, 6, 30))
    after = builder.build(date(2026, 7, 25))

    may_before = next(c for c in before.target_changes if c.month == date(2026, 5, 1))
    may_after = next(c for c in after.target_changes if c.month == date(2026, 5, 1))

    assert may_before.level == pytest.approx(132_624.0)
    assert may_after.level == pytest.approx(132_900.0)
