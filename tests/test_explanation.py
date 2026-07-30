"""Unit tests for the explanation layer.

The load-bearing tests are the honesty ones. A narrative that overclaims, or that
describes a different number than the model produced, is worse than no narrative — so
those failure modes are tested explicitly rather than trusted to review.
"""

from __future__ import annotations

from datetime import date

import pytest

from adp_forecast.config import TARGET_SERIES_ID
from adp_forecast.evaluation.metrics import ScoreCard
from adp_forecast.explanation import (
    Explanation,
    ExplanationError,
    ForecastExplainer,
    explain_forecast,
)
from adp_forecast.forecast import Driver, Forecast, get_model
from forecast_fixtures import make_panel


def driver(
    name: str = "icsa_change",
    label: str = "Initial claims change this month",
    value: float = -19.8,
    contribution: float = -8.5,
    unit_label: str = "thousands of persons",
) -> Driver:
    return Driver(
        name=name,
        label=label,
        value=value,
        contribution=contribution,
        coefficient=-3.2,
        unit_label=unit_label,
    )


def forecast(
    point: float = 85.4,
    drivers: tuple[Driver, ...] = (),
    lower: float | None = -11.3,
    upper: float | None = 127.0,
    baseline_point: float | None = 98.0,
    model_name: str = "ridge",
) -> Forecast:
    return Forecast(
        series_id=TARGET_SERIES_ID,
        month=date(2026, 7, 1),
        as_of=date(2026, 7, 30),
        point=point,
        lower=lower,
        upper=upper,
        interval_level=0.80,
        model_name=model_name,
        drivers=drivers,
        n_train=155,
        baseline_point=baseline_point,
    )


# -- headline and structure ----------------------------------------------------


def test_headline_states_the_month_and_a_job_count():
    explanation = explain_forecast(forecast(point=85.4))

    assert "July 2026" in explanation.headline
    assert "85,000 jobs" in explanation.headline
    assert "gain" in explanation.headline


def test_negative_forecast_reads_as_a_loss():
    explanation = explain_forecast(forecast(point=-42.0))

    assert "loss of 42,000 jobs" in explanation.headline


def test_zero_forecast_reads_as_no_net_change():
    explanation = explain_forecast(forecast(point=0.2))

    assert "no net change" in explanation.headline


def test_counts_are_rounded_to_thousands_like_adp_publishes():
    """ADP headlines to the nearest thousand; implying more precision would be false."""
    explanation = explain_forecast(forecast(point=85.437))

    assert "85,000 jobs" in explanation.headline
    assert "85,437" not in explanation.headline


def test_explanation_renders_to_text():
    explanation = explain_forecast(forecast(drivers=(driver(),)))
    text = explanation.to_text()

    assert explanation.headline in text
    assert "Why:" in text
    assert "Caveats:" in text


def test_explanation_is_structured_not_a_string():
    """Tests assert on fields and a future API serialises them; prose is a rendering."""
    explanation = explain_forecast(forecast(drivers=(driver(),)))

    assert isinstance(explanation, Explanation)
    assert explanation.drivers[0].name == "icsa_change"
    assert explanation.drivers[0].contribution == pytest.approx(-8.5)


# -- intervals -----------------------------------------------------------------


def test_interval_is_stated_with_correct_grammar():
    explanation = explain_forecast(forecast())

    assert explanation.interval is not None
    assert explanation.interval.startswith("An 80%"), "not 'A 80%'"


def test_missing_interval_is_omitted_and_caveated():
    explanation = explain_forecast(forecast(lower=None, upper=None))

    assert explanation.interval is None
    assert any("No range is shown" in caveat for caveat in explanation.caveats)


def test_interval_caveat_states_the_measured_limitation():
    explanation = explain_forecast(forecast())

    assert any("dispersion is stable" in caveat for caveat in explanation.caveats)


# -- drivers -------------------------------------------------------------------


def test_driver_sentence_names_label_value_and_effect():
    explanation = explain_forecast(forecast(drivers=(driver(),)))
    sentence = explanation.drivers[0].sentence

    assert "Initial claims change this month" in sentence
    assert "subtracts" in sentence
    assert "8,000 jobs" in sentence


def test_positive_contribution_reads_as_adding():
    explanation = explain_forecast(forecast(drivers=(driver(contribution=12.0),)))

    assert "adds" in explanation.drivers[0].sentence


def test_driver_values_are_rendered_with_units():
    """A bare '67.3' tells the reader nothing about the scale."""
    explanation = explain_forecast(
        forecast(drivers=(driver(value=202.75, unit_label="thousands of persons"),))
    )

    assert "202,750" in explanation.drivers[0].sentence


def test_percentage_units_are_labelled_not_multiplied():
    explanation = explain_forecast(
        forecast(drivers=(driver(value=4.3, unit_label="percent", contribution=5.0),))
    )

    assert "4.3 percent" in explanation.drivers[0].sentence


def test_immaterial_drivers_are_not_reported_as_reasons():
    """A 0.2k contribution is rounding noise, not an explanation."""
    explanation = explain_forecast(
        forecast(drivers=(driver(contribution=0.2), driver(name="b", contribution=-9.0)))
    )

    assert [statement.name for statement in explanation.drivers] == ["b"]


def test_driver_count_is_configurable():
    drivers = tuple(
        driver(name=f"d{index}", contribution=-10.0 * (index + 1)) for index in range(5)
    )
    explanation = ForecastExplainer(driver_count=2).explain(forecast(drivers=drivers))

    assert len(explanation.drivers) == 2


def test_driver_count_must_be_positive():
    with pytest.raises(ValueError, match="driver_count"):
        ForecastExplainer(driver_count=0)


def test_no_drivers_yields_an_honest_placeholder():
    explanation = explain_forecast(forecast(model_name="random_walk"))

    assert explanation.drivers == ()
    assert "No individual driver" in explanation.to_text()


def test_baseline_anchor_says_no_model_was_fitted():
    explanation = explain_forecast(forecast(model_name="mean_3m"))

    assert "no model fitted" in explanation.anchor


# -- the consistency guard -----------------------------------------------------


def test_drivers_summing_to_an_implausible_intercept_are_refused():
    """The narrative must never describe a different number than the model produced."""
    bogus = forecast(point=85.4, drivers=(driver(contribution=-5e6),))

    with pytest.raises(ExplanationError, match="does not match"):
        explain_forecast(bogus)


def test_a_consistent_forecast_explains_cleanly():
    explanation = explain_forecast(forecast(drivers=(driver(contribution=-8.5),)))

    assert explanation.anchor.startswith("Start from")


def test_anchor_reports_the_implied_training_mean():
    """Anchor plus contributions must reconstruct the point forecast."""
    explanation = explain_forecast(
        forecast(point=85.4, drivers=(driver(contribution=-70.0),))
    )

    assert "155,000 jobs" in explanation.anchor


# -- comparison and context ----------------------------------------------------


def test_comparison_measures_against_last_months_print():
    explanation = explain_forecast(forecast(point=53.0, baseline_point=98.0))

    assert "45,000 jobs below" in explanation.comparison
    assert "98,000 jobs" in explanation.comparison


def test_comparison_notes_when_the_forecast_repeats_last_month():
    explanation = explain_forecast(forecast(point=98.2, baseline_point=98.0))

    assert "essentially last month" in explanation.comparison


def test_missing_baseline_is_stated_not_invented():
    explanation = explain_forecast(forecast(baseline_point=None))

    assert "No prior month" in explanation.comparison


def test_context_uses_recent_history_when_a_panel_is_supplied():
    panel = make_panel()
    explanation = explain_forecast(forecast(), panel)

    assert "prints averaged" in explanation.context


def test_context_degrades_gracefully_without_a_panel():
    explanation = explain_forecast(forecast())

    assert "2026-07-30" in explanation.context


# -- honesty -------------------------------------------------------------------


def test_accuracy_caveat_is_not_hardcoded():
    """A caveat quoting stale figures is worse than one that defers to the backtest."""
    explanation = explain_forecast(forecast())

    caveat = explanation.caveats[0]
    assert "adp-forecast backtest" in caveat
    assert "62,000" not in caveat


def test_no_user_facing_text_points_at_the_deprecated_scripts():
    """The scripts are shims; every instruction must name the CLI command instead.

    A guard rather than a one-off fix: this exact string survived the CLI migration
    because it lived in prose no test was reading.
    """
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path.name
        for path in source_root.rglob("*.py")
        if "scripts/" in path.read_text()
    ]

    assert not offenders, f"stale scripts/ reference in: {offenders}"


def test_supplied_accuracy_is_quoted_and_verdict_reflects_it():
    model = ScoreCard("ridge", 39, 62.1, 88.0, 3.0, 0.95, 0.85, 256.0, 39)
    baseline = ScoreCard("mean_3m", 39, 63.4, 84.6, 7.6, 0.95, 0.92, 319.0, 39)

    explanation = ForecastExplainer(
        accuracy=model, baseline_accuracy=baseline
    ).explain(forecast())

    caveat = explanation.caveats[0]
    assert "62,000 jobs" in caveat
    assert "63,000" in caveat
    assert "not clearly better" in caveat


def test_a_losing_model_describes_itself_as_losing():
    """The wording follows the numbers, so it cannot flatter a model that lost."""
    model = ScoreCard("ridge", 39, 80.0, 90.0, 3.0, 0.9, 0.85, 256.0, 39)
    baseline = ScoreCard("mean_3m", 39, 60.0, 84.6, 7.6, 0.95, 0.92, 319.0, 39)

    explanation = ForecastExplainer(
        accuracy=model, baseline_accuracy=baseline
    ).explain(forecast())

    assert "not clearly better" in explanation.caveats[0]


def test_a_clearly_better_model_may_say_so():
    model = ScoreCard("ridge", 39, 40.0, 55.0, 1.0, 0.95, 0.80, 200.0, 39)
    baseline = ScoreCard("mean_3m", 39, 63.4, 84.6, 7.6, 0.95, 0.92, 319.0, 39)

    explanation = ForecastExplainer(
        accuracy=model, baseline_accuracy=baseline
    ).explain(forecast())

    assert "clear improvement" in explanation.caveats[0]


def test_drivers_are_described_as_association_not_causation():
    explanation = explain_forecast(forecast(drivers=(driver(),)))

    assert any("not causation" in caveat for caveat in explanation.caveats)
    assert not any("causes" in caveat for caveat in explanation.caveats)


def test_excluded_regime_is_disclosed():
    explanation = explain_forecast(forecast(drivers=(driver(),)))

    assert any("2020" in caveat and "excluded" in caveat for caveat in explanation.caveats)


def test_stale_inputs_are_disclosed():
    """A reader assuming every input is current would over-trust the number."""
    panel = make_panel()
    explanation = explain_forecast(forecast(drivers=(driver(),)), panel)

    stale = [caveat for caveat in explanation.caveats if "behind" in caveat]
    assert stale
    assert "Job openings" in stale[0]


def test_no_caveat_claims_the_forecast_is_reliable():
    explanation = explain_forecast(forecast(drivers=(driver(),)), make_panel())
    text = explanation.to_text().lower()

    for overclaim in ("highly accurate", "reliable prediction", "guarantee"):
        assert overclaim not in text


# -- integration ---------------------------------------------------------------


def test_a_real_ridge_forecast_explains_end_to_end():
    panel = make_panel()
    result = get_model("ridge").forecast(panel)

    explanation = explain_forecast(result, panel)

    assert explanation.drivers, "a fitted model must attribute its forecast"
    assert explanation.forecast is result
    assert explanation.to_text()


@pytest.mark.parametrize("model_name", ["ridge", "random_walk", "mean_3m", "drift"])
def test_every_model_can_be_explained(model_name):
    panel = make_panel()
    result = get_model(model_name).forecast(panel)

    assert explain_forecast(result, panel).to_text()
