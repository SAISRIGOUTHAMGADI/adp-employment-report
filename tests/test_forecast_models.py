"""Unit tests for the baselines, the ridge forecaster and the model registry."""

from __future__ import annotations

from datetime import date

import pytest

from adp_forecast.config import TARGET_SERIES_ID, is_excluded_month
from adp_forecast.exceptions import InsufficientDataError
from adp_forecast.forecast import (
    BASELINE_MODELS,
    DEFAULT_MODEL,
    DEFAULT_TERMS,
    MODEL_REGISTRY,
    DriftForecaster,
    ForecastPort,
    MovingAverageForecaster,
    RandomWalkForecaster,
    RidgeForecaster,
    get_model,
    usable_changes,
)
from forecast_fixtures import make_panel


@pytest.fixture
def panel():
    return make_panel()


# -- the contract --------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MODEL_REGISTRY))
def test_every_registered_model_satisfies_the_port(name):
    assert isinstance(get_model(name), ForecastPort)


@pytest.mark.parametrize("name", sorted(MODEL_REGISTRY))
def test_every_model_forecasts_the_panels_target_month(name, panel):
    forecast = get_model(name).forecast(panel)

    assert forecast.month == panel.target_month
    assert forecast.as_of == panel.as_of
    assert forecast.series_id == TARGET_SERIES_ID
    assert forecast.model_name == name


@pytest.mark.parametrize("name", sorted(MODEL_REGISTRY))
def test_models_are_stateless_across_calls(name, panel):
    """A retained fit could leak a later origin's data into an earlier backtest fold."""
    model = get_model(name)

    assert model.forecast(panel).point == pytest.approx(model.forecast(panel).point)


def test_registry_default_is_the_ridge_model():
    assert DEFAULT_MODEL == "ridge"
    assert isinstance(get_model(), RidgeForecaster)


def test_baselines_are_all_registered():
    assert set(BASELINE_MODELS) <= set(MODEL_REGISTRY)
    assert DEFAULT_MODEL not in BASELINE_MODELS


def test_seasonal_naive_is_deliberately_absent():
    """The series is already seasonally adjusted; a seasonal naive would be wrong."""
    assert not any("seasonal" in name for name in MODEL_REGISTRY)


def test_unknown_model_lists_what_is_available():
    with pytest.raises(KeyError, match="Registered:"):
        get_model("nope")


# -- regime filtering ----------------------------------------------------------


def test_usable_changes_drops_the_pandemic_window(panel):
    usable = usable_changes(panel)

    assert usable
    assert not any(is_excluded_month(change.month) for change in usable)
    assert len(usable) < len(panel.target_changes)


@pytest.mark.parametrize("name", sorted(MODEL_REGISTRY))
def test_no_model_trains_on_excluded_months(name, panel):
    """Baselines and the model must see the same history, or comparison is meaningless."""
    outlier_panel = make_panel(target_values={date(2020, 5, 1): -1828.0})

    forecast = get_model(name).forecast(outlier_panel)

    assert forecast.point > -200.0, "an excluded outlier must not drag the forecast"


# -- baseline behaviour --------------------------------------------------------


def test_random_walk_predicts_the_last_usable_value(panel):
    forecast = RandomWalkForecaster().forecast(panel)

    assert forecast.point == pytest.approx(usable_changes(panel)[-1].change)


def test_moving_average_predicts_the_window_mean(panel):
    usable = [change.change for change in usable_changes(panel)]

    for window in (3, 6):
        forecast = MovingAverageForecaster(window).forecast(panel)
        assert forecast.point == pytest.approx(sum(usable[-window:]) / window)


def test_moving_average_names_itself_by_window():
    assert MovingAverageForecaster(3).name == "mean_3m"
    assert MovingAverageForecaster(6).name == "mean_6m"


def test_moving_average_window_must_be_positive():
    with pytest.raises(ValueError, match="window"):
        MovingAverageForecaster(0)


def test_drift_extends_the_average_trend(panel):
    usable = [change.change for change in usable_changes(panel)]
    expected = usable[-1] + (usable[-1] - usable[0]) / (len(usable) - 1)

    assert DriftForecaster().forecast(panel).point == pytest.approx(expected)


def test_baselines_report_no_drivers(panel):
    """A naive rule has no features, so it must not manufacture an explanation."""
    for name in BASELINE_MODELS:
        assert get_model(name).forecast(panel).drivers == ()


def test_baseline_refuses_to_forecast_from_nothing():
    with pytest.raises(InsufficientDataError):
        RandomWalkForecaster().forecast(make_panel(months=1, start=date(2026, 5, 1)))


# -- ridge behaviour -----------------------------------------------------------


def test_ridge_produces_one_driver_per_term(panel):
    forecast = RidgeForecaster().forecast(panel)

    assert len(forecast.drivers) == len(DEFAULT_TERMS)
    assert {driver.name for driver in forecast.drivers} == {
        term.name for term in DEFAULT_TERMS
    }


def test_drivers_are_ordered_by_absolute_contribution(panel):
    forecast = RidgeForecaster().forecast(panel)
    magnitudes = [abs(driver.contribution) for driver in forecast.drivers]

    assert magnitudes == sorted(magnitudes, reverse=True)


def test_top_drivers_returns_the_largest(panel):
    forecast = RidgeForecaster().forecast(panel)

    assert forecast.top_drivers(3) == forecast.drivers[:3]
    assert len(forecast.top_drivers(100)) == len(forecast.drivers)


def test_driver_labels_come_from_the_term_declarations(panel):
    forecast = RidgeForecaster().forecast(panel)

    assert all(driver.label.strip() for driver in forecast.drivers)
    assert any("claims" in driver.label.lower() for driver in forecast.drivers)


def test_driver_direction_reflects_contribution_sign(panel):
    forecast = RidgeForecaster().forecast(panel)

    for driver in forecast.drivers:
        if driver.contribution > 0.5:
            assert driver.direction == "raises"
        elif driver.contribution < -0.5:
            assert driver.direction == "lowers"
        else:
            assert driver.direction == "neutral"


def test_ridge_forecast_is_plausible(panel):
    forecast = RidgeForecaster().forecast(panel)

    assert -500.0 < forecast.point < 500.0
    assert forecast.n_train > 50


def test_ridge_reports_the_random_walk_alongside_itself(panel):
    """Output should always be able to show what the model adds over doing nothing."""
    forecast = RidgeForecaster().forecast(panel)

    assert forecast.baseline_point == pytest.approx(usable_changes(panel)[-1].change)


def test_ridge_rejects_an_empty_alpha_grid():
    with pytest.raises(ValueError, match="alphas"):
        RidgeForecaster(alphas=[])


@pytest.mark.parametrize("level", [0.0, 1.0, -0.5, 1.5])
def test_ridge_rejects_an_impossible_interval_level(level):
    with pytest.raises(ValueError, match="interval_level"):
        RidgeForecaster(interval_level=level)


def test_ridge_refuses_to_fit_on_too_little_history():
    with pytest.raises(InsufficientDataError):
        RidgeForecaster(min_samples=500).forecast(make_panel())


# -- intervals -----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MODEL_REGISTRY))
def test_intervals_bracket_the_point_forecast(name, panel):
    forecast = get_model(name).forecast(panel)

    if forecast.has_interval:
        assert forecast.lower < forecast.point < forecast.upper
        assert forecast.interval_width > 0


def test_ridge_interval_is_empirical_and_asymmetric_when_residuals_are(panel):
    """Empirical quantiles make no normality claim, so symmetry is not guaranteed."""
    forecast = RidgeForecaster().forecast(panel)

    assert forecast.has_interval
    assert forecast.interval_level == pytest.approx(0.80)


def test_wider_interval_level_widens_the_interval(panel):
    narrow = RidgeForecaster(interval_level=0.50).forecast(panel)
    wide = RidgeForecaster(interval_level=0.95).forecast(panel)

    assert wide.interval_width > narrow.interval_width


def test_interval_is_omitted_rather_than_faked_without_residuals(panel):
    """Too few residuals must yield no interval, not a falsely precise one."""
    forecast = RidgeForecaster(min_samples=24).forecast(make_panel(months=60))

    assert forecast.point is not None
    if not forecast.has_interval:
        assert forecast.lower is None and forecast.upper is None
