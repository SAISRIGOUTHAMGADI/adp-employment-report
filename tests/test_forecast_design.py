"""Unit tests for design-matrix construction.

The lag-availability checks matter most: they are what turn "used a figure that had not
been published yet" from an invisible scoring inflation into a construction-time error.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from adp_forecast.config import (
    COVID_EXCLUSION_END,
    COVID_EXCLUSION_START,
    TARGET_SERIES_ID,
    get_series_spec,
    is_excluded_month,
)
from adp_forecast.exceptions import ConfigurationError, InsufficientDataError
from adp_forecast.forecast import DEFAULT_TERMS, FeatureTerm, Transform, build_design_matrix
from forecast_fixtures import make_panel, shift_months


# -- regime exclusion ----------------------------------------------------------


def test_exclusion_window_matches_the_measured_boundary():
    assert COVID_EXCLUSION_START == date(2020, 3, 1)
    assert COVID_EXCLUSION_END == date(2022, 6, 1)
    assert is_excluded_month(date(2020, 3, 1))
    assert is_excluded_month(date(2021, 12, 1)), "the reopening boom is excluded too"
    assert is_excluded_month(date(2022, 6, 15)), "any day in the month counts"
    assert not is_excluded_month(date(2020, 2, 1))
    assert not is_excluded_month(date(2022, 7, 1))


def test_pandemic_months_are_dropped_from_training():
    design = build_design_matrix(make_panel())

    assert not any(is_excluded_month(month) for month in design.months)
    assert design.excluded_months, "the window overlaps the generated history"


def test_rows_whose_lagged_features_fall_in_the_window_are_also_dropped():
    """A clean target month fed by pandemic-era claims is still contaminated."""
    design = build_design_matrix(make_panel())
    excluded = set(design.excluded_months)

    # Terms reach back up to lag 3, plus one more month for a change.
    just_after = shift_months(COVID_EXCLUSION_END, 1)
    assert just_after in excluded, "the month after the window still draws on it"
    assert shift_months(COVID_EXCLUSION_END, 6) not in excluded


def test_an_outlier_inside_the_window_never_reaches_training():
    """The -1,828k May 2020 print must be absent, not clipped."""
    panel = make_panel(target_values={date(2020, 5, 1): -1828.0})
    design = build_design_matrix(panel)

    assert date(2020, 5, 1) not in design.months
    assert float(design.y.min()) > -500.0


# -- lag availability ----------------------------------------------------------


def test_default_terms_respect_every_registered_publication_lag():
    for term in DEFAULT_TERMS:
        spec = get_series_spec(term.series_id)
        minimum = max(
            spec.publication_lag_months,
            1 if term.series_id == TARGET_SERIES_ID else 0,
        )
        assert term.lag_months >= minimum, f"{term.name} would read unpublished data"


def test_jolts_is_declared_at_its_real_two_month_lag():
    jolts = next(t for t in DEFAULT_TERMS if t.series_id == "JTSJOL")
    assert jolts.lag_months == 2


def test_claims_are_usable_at_lag_zero():
    """Weekly claims for month T are fully published before ADP's release for T."""
    icsa = [t for t in DEFAULT_TERMS if t.series_id == "ICSA"]
    assert icsa and all(term.lag_months == 0 for term in icsa)


def test_a_term_below_its_publication_lag_is_refused():
    leaky = FeatureTerm("jolts_now", "JTSJOL", Transform.CHANGE, 0, "JOLTS this month")

    with pytest.raises(ConfigurationError, match="leak unpublished data"):
        build_design_matrix(make_panel(), [leaky])


def test_bls_payrolls_at_lag_zero_are_refused():
    """BLS publishes two days after ADP, so month T is not available at forecast time."""
    leaky = FeatureTerm("usprv_now", "USPRIV", Transform.CHANGE, 0, "BLS now")

    with pytest.raises(ConfigurationError, match="leak unpublished data"):
        build_design_matrix(make_panel(), [leaky])


def test_the_target_at_lag_zero_is_refused():
    """Lag 0 on the target is the answer itself."""
    leaky = FeatureTerm("adp_now", TARGET_SERIES_ID, Transform.CHANGE, 0, "ADP now")

    with pytest.raises(ConfigurationError, match="leak unpublished data"):
        build_design_matrix(make_panel(), [leaky])


def test_a_negative_lag_is_refused():
    future = FeatureTerm("icsa_next", "ICSA", Transform.CHANGE, -1, "next month")

    with pytest.raises(ConfigurationError, match="read the future"):
        build_design_matrix(make_panel(), [future])


def test_the_target_level_is_refused_as_a_regressor():
    """The level is rebenchmark-dependent; only its change is meaningful."""
    level = FeatureTerm("adp_level", TARGET_SERIES_ID, Transform.LEVEL, 1, "ADP level")

    with pytest.raises(ConfigurationError, match="only usable as a change"):
        build_design_matrix(make_panel(), [level])


# -- shape and alignment -------------------------------------------------------


def test_matrix_shape_matches_rows_and_terms():
    design = build_design_matrix(make_panel())

    assert design.x.shape == (design.n_samples, design.n_terms)
    assert design.y.shape == (design.n_samples,)
    assert design.x_next.shape == (design.n_terms,)
    assert len(design.months) == design.n_samples


def test_targets_align_with_their_months():
    panel = make_panel()
    design = build_design_matrix(panel)
    by_month = {change.month: change.change for change in panel.target_changes}

    for index, month in enumerate(design.months):
        assert design.y[index] == pytest.approx(by_month[month])


def test_lagged_column_holds_the_previous_months_value():
    panel = make_panel()
    design = build_design_matrix(panel)
    by_month = {change.month: change.change for change in panel.target_changes}
    column = design.terms.index(
        next(t for t in design.terms if t.name == "adp_change_lag1")
    )

    for index, month in enumerate(design.months):
        expected = by_month[shift_months(month, -1)]
        assert design.x[index, column] == pytest.approx(expected)


def test_prediction_row_uses_the_target_month(monkeypatch):
    panel = make_panel()
    design = build_design_matrix(panel)
    by_month = {change.month: change.change for change in panel.target_changes}
    column = design.terms.index(
        next(t for t in design.terms if t.name == "adp_change_lag1")
    )

    assert design.target_month == panel.target_month
    assert design.x_next[column] == pytest.approx(
        by_month[shift_months(panel.target_month, -1)]
    )


def test_training_months_are_ordered():
    design = build_design_matrix(make_panel())

    assert list(design.months) == sorted(design.months)


def test_matrix_is_finite():
    design = build_design_matrix(make_panel())

    assert np.all(np.isfinite(design.x))
    assert np.all(np.isfinite(design.y))
    assert np.all(np.isfinite(design.x_next))


# -- missing data --------------------------------------------------------------


def test_months_missing_a_feature_are_recorded_not_silently_dropped():
    design = build_design_matrix(make_panel())

    total = len(design.months) + len(design.excluded_months) + len(design.incomplete_months)
    assert total >= design.n_samples
    assert set(design.months).isdisjoint(design.incomplete_months)


def test_insufficient_history_raises_with_a_useful_message():
    with pytest.raises(InsufficientDataError, match="complete training row"):
        build_design_matrix(make_panel(months=30), min_samples=100)


def test_an_unavailable_feature_blocks_the_forecast_explicitly():
    """A feature three months staler than declared must fail loudly, not silently."""
    panel = make_panel(feature_lag_gaps={"JTSJOL": 3})

    with pytest.raises(InsufficientDataError, match="jolts_change"):
        build_design_matrix(panel)


def test_custom_term_list_is_honoured():
    terms = [
        FeatureTerm("adp_lag1", TARGET_SERIES_ID, Transform.CHANGE, 1, "ADP lag 1"),
        FeatureTerm("icsa_level", "ICSA", Transform.LEVEL, 0, "Claims level"),
    ]
    design = build_design_matrix(make_panel(), terms)

    assert design.n_terms == 2
    assert [term.name for term in design.terms] == ["adp_lag1", "icsa_level"]


# -- trailing mean -------------------------------------------------------------


def test_trailing_mean_is_the_first_default_term():
    """It exists to fix the diagnosed intercept anchoring, so it leads the list."""
    assert DEFAULT_TERMS[0].name == "adp_trailing_mean_12"
    assert DEFAULT_TERMS[0].transform is Transform.TRAILING_MEAN
    assert DEFAULT_TERMS[0].window == 12
    assert DEFAULT_TERMS[0].lag_months == 1


def test_trailing_mean_averages_the_declared_window():
    panel = make_panel()
    by_month = {c.month: c.change for c in panel.target_changes}
    design = build_design_matrix(panel)
    column = [t.name for t in design.terms].index("adp_trailing_mean_12")

    month = design.months[-1]
    window = [
        by_month[shift_months(month, -1 - offset)]
        for offset in range(12)
        if not is_excluded_month(shift_months(month, -1 - offset))
    ]
    assert design.x[-1, column] == pytest.approx(sum(window) / len(window))


def test_trailing_mean_skips_excluded_months_instead_of_dropping_the_row():
    """Requiring 12 clean months would delete a year of scarce post-regime data."""
    panel = make_panel(target_values={date(2021, 12, 1): 947.0})
    design = build_design_matrix(panel)
    column = [t.name for t in design.terms].index("adp_trailing_mean_12")

    just_after = shift_months(COVID_EXCLUSION_END, 7)
    assert just_after in design.months, "the row survives the pandemic window"
    row = design.months.index(just_after)
    assert design.x[row, column] < 300.0, "the +947k month never entered the average"


def test_trailing_mean_below_min_periods_is_unavailable():
    """A one-or-two-month anchor is noise; report it absent rather than compute it."""
    term = FeatureTerm(
        "anchor", TARGET_SERIES_ID, Transform.TRAILING_MEAN, 1, "anchor", window=12
    )
    # Target history ends 2021-06, so the forecast month is 2021-07 and its whole
    # 12-month window (2020-07..2021-06) sits inside the exclusion window.
    panel = make_panel(months=30, start=date(2019, 1, 1))
    assert panel.target_month == date(2021, 7, 1)

    with pytest.raises(InsufficientDataError, match="anchor"):
        build_design_matrix(panel, [term], min_samples=1)


def test_trailing_mean_window_must_exceed_one():
    term = FeatureTerm(
        "anchor", TARGET_SERIES_ID, Transform.TRAILING_MEAN, 1, "anchor", window=1
    )
    with pytest.raises(ConfigurationError, match="use Transform.CHANGE"):
        build_design_matrix(make_panel(), [term])


def test_unsatisfiable_min_periods_is_refused():
    term = FeatureTerm(
        "anchor", TARGET_SERIES_ID, Transform.TRAILING_MEAN, 1, "anchor",
        window=6, min_periods=99,
    )
    with pytest.raises(ConfigurationError, match="never be satisfied"):
        build_design_matrix(make_panel(), [term])


def test_trailing_mean_is_not_supported_for_feature_series():
    term = FeatureTerm(
        "claims_anchor", "ICSA", Transform.TRAILING_MEAN, 0, "claims", window=6
    )
    with pytest.raises(ConfigurationError, match="only implemented for the target"):
        build_design_matrix(make_panel(), [term])


def test_required_periods_defaults_to_half_the_window():
    assert FeatureTerm("a", TARGET_SERIES_ID, Transform.TRAILING_MEAN, 1, "a",
                       window=12).required_periods == 6
    assert FeatureTerm("a", TARGET_SERIES_ID, Transform.TRAILING_MEAN, 1, "a",
                       window=7).required_periods == 4
