"""Unit tests for metrics and the walk-forward backtester."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.config import TARGET_SERIES_ID
from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, Observation
from adp_forecast.evaluation import (
    Backtester,
    Scorecard,
    directional_accuracy,
    interval_coverage,
    mean_absolute_error,
    mean_error,
    root_mean_squared_error,
    score,
)
from adp_forecast.exceptions import InsufficientDataError
from adp_forecast.storage import SqliteStorage

FETCHED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)


# -- metrics -------------------------------------------------------------------


def test_error_metrics_on_known_values():
    errors = [3.0, -4.0, 0.0]

    assert mean_absolute_error(errors) == pytest.approx(7.0 / 3.0)
    assert root_mean_squared_error(errors) == pytest.approx((25.0 / 3.0) ** 0.5)
    assert mean_error(errors) == pytest.approx(-1.0 / 3.0)


def test_rmse_exceeds_mae_when_errors_are_uneven():
    """The reason both are reported: they can rank models differently."""
    errors = [0.0, 0.0, 0.0, 100.0]

    assert root_mean_squared_error(errors) > mean_absolute_error(errors)


def test_bias_sign_means_forecasting_too_high():
    assert mean_error([10.0, 10.0]) > 0, "error = forecast - actual"


def test_directional_accuracy_counts_matching_signs():
    assert directional_accuracy([1.0, -1.0, 2.0], [5.0, -5.0, -5.0]) == pytest.approx(2 / 3)


def test_directional_accuracy_skips_zero_actuals():
    """A zero actual has no direction to get right."""
    assert directional_accuracy([1.0, 1.0], [0.0, 5.0]) == pytest.approx(1.0)
    assert directional_accuracy([1.0], [0.0]) is None


def test_interval_coverage_counts_inclusive_bounds():
    coverage, width, n = interval_coverage(
        [5.0, 15.0, 0.0], [0.0, 0.0, 0.0], [10.0, 10.0, 0.0]
    )

    assert coverage == pytest.approx(2 / 3)
    assert width == pytest.approx(20.0 / 3)
    assert n == 3


def test_interval_coverage_ignores_forecasts_without_bounds():
    coverage, _width, n = interval_coverage([5.0, 5.0], [0.0, None], [10.0, None])

    assert coverage == pytest.approx(1.0)
    assert n == 1


def test_interval_coverage_is_none_when_no_intervals_exist():
    assert interval_coverage([1.0], [None], [None]) == (None, None, 0)


def test_coverage_gap_is_negative_when_under_covering():
    """Under-covering is the failure mode: claiming precision the model lacks."""
    card = score("m", [1.0] * 10, [1.0] * 10, [0.0] * 10, [2.0] * 10)

    assert card.interval_coverage == pytest.approx(1.0)
    assert card.coverage_gap(0.80) == pytest.approx(0.20)

    narrow = score("m", [1.0] * 10, [5.0] * 10, [0.0] * 10, [2.0] * 10)
    assert narrow.coverage_gap(0.80) == pytest.approx(-0.80)


def test_score_rejects_misaligned_inputs():
    with pytest.raises(ValueError, match="forecasts vs"):
        score("m", [1.0, 2.0], [1.0])


def test_score_rejects_an_empty_run():
    with pytest.raises(InsufficientDataError):
        score("m", [], [])


def test_mape_is_not_offered():
    """Deliberately absent: the target passes near zero, so MAPE would explode."""
    from adp_forecast.evaluation import metrics

    assert not hasattr(metrics, "mean_absolute_percentage_error")


# -- backtester ----------------------------------------------------------------


def observation(obs_date: date, value: float, realtime_start: date) -> Observation:
    return Observation(
        series_id=TARGET_SERIES_ID,
        date=obs_date,
        value=value,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=realtime_start,
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    )


@pytest.fixture
def store():
    """A store with monthly ADP levels and matching release dates."""
    with SqliteStorage(":memory:") as instance:
        instance.initialise()

        observations = []
        releases = []
        level = 130_000_000.0
        month = date(2023, 1, 1)
        for index in range(30):
            level += (100.0 + 20.0 * ((index % 5) - 2)) * 1000.0
            release = _first_of_next_month(month)
            observations.append(observation(month, level, release))
            releases.append(release)
            month = _first_of_next_month(month)

        instance.upsert_observations(observations)
        instance.upsert_release_dates(194, releases)
        yield instance


def _first_of_next_month(value: date) -> date:
    total = value.year * 12 + value.month
    return date(total // 12, total % 12 + 1, 1)


def test_backtest_scores_the_naive_models(store):
    report = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk", "mean_3m"), today=date(2026, 7, 30)
    )

    assert report.n_scored > 0
    assert set(report.scores) == {"random_walk", "mean_3m"}
    assert all(card.n == report.n_scored for card in report.scores.values())


def test_all_models_are_scored_on_the_same_origins(store):
    """Scoring models over different subsets is not a comparison."""
    report = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk", "drift"), today=date(2026, 7, 30)
    )

    counts = {card.n for card in report.scores.values()}
    assert len(counts) == 1


def test_dropped_origins_are_reported_not_hidden(store):
    report = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk",), today=date(2026, 7, 30)
    )

    assert report.n_attempted >= report.n_scored
    assert report.n_dropped == report.n_attempted - report.n_scored


def test_future_release_dates_are_excluded(store):
    """FRED returns scheduled future dates; scoring them would be forecasting nothing."""
    early = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk",), today=date(2024, 1, 1)
    )
    late = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk",), today=date(2026, 7, 30)
    )

    assert early.n_scored < late.n_scored
    assert max(early.common_origins) <= date(2024, 1, 1)


def test_best_by_mae_picks_the_lowest(store):
    report = Backtester(store).run(
        Scorecard.VINTAGE,
        models=("random_walk", "mean_3m", "mean_6m"),
        today=date(2026, 7, 30),
    )

    best = report.best_by_mae()
    assert all(
        report.scores[best].mae <= card.mae for card in report.scores.values()
    )


def test_relative_mae_is_positive_when_better(store):
    report = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk", "mean_3m"), today=date(2026, 7, 30)
    )
    better = report.best_by_mae()
    other = next(name for name in report.models if name != better)

    assert report.relative_mae(better, other) > 0
    assert report.relative_mae(other, better) < 0


def test_actual_is_read_at_the_release_date_not_today(store):
    """Scoring against a later revision measures rebenchmark luck, not forecasting."""
    report = Backtester(store).run(
        Scorecard.VINTAGE, models=("random_walk",), today=date(2026, 7, 30)
    )
    outcome = report.outcomes[0]
    levels = {
        obs.date: obs.value
        for obs in store.read_observations(TARGET_SERIES_ID, as_of=outcome.origin)
        if obs.value is not None
    }

    assert outcome.target_month in levels, "the actual came from that day's snapshot"


def test_empty_store_raises_rather_than_reporting_a_vacuous_score():
    with SqliteStorage(":memory:") as empty:
        empty.initialise()
        with pytest.raises(InsufficientDataError):
            Backtester(empty).run(Scorecard.VINTAGE, models=("random_walk",))


def test_scorecard_names_are_stable():
    """Persisted in reports, so renaming would silently invalidate saved results."""
    assert Scorecard.VINTAGE.value == "vintage"
    assert Scorecard.LAG_SHIFTED.value == "lag_shifted"
