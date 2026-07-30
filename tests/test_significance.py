"""Unit tests for paired forecast-accuracy testing.

Two things are being verified. First that the hand-rolled Student's *t* distribution is
correct — validated against published critical values, since a wrong p-value would be
worse than none. Second that the test refuses to call a noisy difference a win, which is
the entire reason it exists.
"""

from __future__ import annotations

import math
import random

import pytest

from adp_forecast.evaluation.significance import (
    ALPHA,
    MIN_PAIRS,
    ComparisonResult,
    Loss,
    _regularised_incomplete_beta,
    _student_t_two_sided_p,
    diebold_mariano,
)
from adp_forecast.exceptions import InsufficientDataError

RNG = random.Random(20260730)


# -- Student's t, against published critical values ----------------------------


@pytest.mark.parametrize(
    "critical, df",
    [
        (12.706, 1),    # two-sided 5% critical values from standard t tables
        (4.303, 2),
        (2.776, 4),
        (2.228, 10),
        (2.086, 20),
        (2.024, 38),    # the degrees of freedom this project's 39 origins produce
        (1.960, 100_000),
    ],
)
def test_t_distribution_matches_published_critical_values(critical, df):
    """At the 5% critical value the two-sided p must be 0.05."""
    assert _student_t_two_sided_p(critical, degrees_of_freedom=df) == pytest.approx(
        0.05, abs=1e-3
    )


def test_zero_statistic_is_certainly_not_significant():
    assert _student_t_two_sided_p(0.0, degrees_of_freedom=38) == pytest.approx(1.0)


def test_p_value_is_symmetric_in_sign():
    positive = _student_t_two_sided_p(1.7, degrees_of_freedom=38)
    negative = _student_t_two_sided_p(-1.7, degrees_of_freedom=38)

    assert positive == pytest.approx(negative)


def test_p_value_decreases_as_the_statistic_grows():
    values = [
        _student_t_two_sided_p(t, degrees_of_freedom=38) for t in (0.5, 1.0, 2.0, 4.0)
    ]

    assert values == sorted(values, reverse=True)
    assert all(0.0 <= value <= 1.0 for value in values)


def test_large_df_converges_to_the_normal():
    """At 100k df the t distribution is the normal to three decimals."""
    normal_p = math.erfc(1.96 / math.sqrt(2.0))
    assert _student_t_two_sided_p(1.96, degrees_of_freedom=100_000) == pytest.approx(
        normal_p, abs=1e-3
    )


def test_invalid_degrees_of_freedom_is_rejected():
    with pytest.raises(ValueError, match="degrees_of_freedom"):
        _student_t_two_sided_p(1.0, degrees_of_freedom=0)


# -- the incomplete beta underneath it -----------------------------------------


def test_incomplete_beta_boundaries():
    assert _regularised_incomplete_beta(2.0, 3.0, 0.0) == 0.0
    assert _regularised_incomplete_beta(2.0, 3.0, 1.0) == 1.0


def test_incomplete_beta_symmetry_identity():
    """I_x(a,b) = 1 - I_{1-x}(b,a) must hold, and exercises both fraction branches."""
    for x in (0.1, 0.3, 0.5, 0.7, 0.9):
        left = _regularised_incomplete_beta(2.5, 3.5, x)
        right = 1.0 - _regularised_incomplete_beta(3.5, 2.5, 1.0 - x)
        assert left == pytest.approx(right, abs=1e-12)


def test_incomplete_beta_reduces_to_the_uniform_case():
    """I_x(1,1) = x exactly."""
    for x in (0.05, 0.25, 0.5, 0.75, 0.95):
        assert _regularised_incomplete_beta(1.0, 1.0, x) == pytest.approx(x, abs=1e-12)


def test_incomplete_beta_is_monotone():
    values = [_regularised_incomplete_beta(2.0, 5.0, x) for x in (0.1, 0.2, 0.4, 0.8)]

    assert values == sorted(values)


# -- the test's behaviour ------------------------------------------------------


def test_identical_models_are_indistinguishable():
    errors = [RNG.gauss(0, 50) for _ in range(39)]

    with pytest.raises(InsufficientDataError, match="identical losses"):
        diebold_mariano(errors, list(errors))


def test_a_tiny_difference_on_a_small_sample_is_not_significant():
    """The case this project is actually in: a 2% MAE gap on 39 noisy origins."""
    baseline = [RNG.gauss(0, 88) for _ in range(39)]
    model = [error * 0.98 + RNG.gauss(0, 60) for error in baseline]

    result = diebold_mariano(model, baseline, model_name="ridge", baseline_name="mean_3m")

    assert not result.is_significant
    assert "indistinguishable" in result.verdict


def test_a_large_consistent_difference_is_detected():
    """The test must still have power, or a non-result would mean nothing."""
    baseline = [RNG.gauss(0, 80) for _ in range(39)]
    model = [error * 0.3 for error in baseline]

    result = diebold_mariano(model, baseline)

    assert result.is_significant
    assert result.model_is_better
    assert result.p_value < ALPHA


def test_a_worse_model_is_reported_as_worse():
    baseline = [RNG.gauss(0, 40) for _ in range(50)]
    model = [error * 3.0 for error in baseline]

    result = diebold_mariano(model, baseline)

    assert not result.model_is_better
    assert result.mean_differential > 0
    assert "higher" in result.verdict


def test_verdict_never_claims_a_win_without_significance():
    """The whole point: a 2% gap must not be reported as an improvement."""
    baseline = [RNG.gauss(0, 88) for _ in range(39)]
    # A marginally better model, but noisily so — mean loss is lower while the
    # per-origin differential varies widely, which is the real-world situation.
    model = [error * 0.97 + RNG.gauss(0, 55) for error in baseline]

    result = diebold_mariano(model, baseline, model_name="ridge")

    assert not result.is_significant
    assert "significantly" not in result.verdict
    assert "indistinguishable" in result.verdict


# -- loss functions ------------------------------------------------------------


#: Deliberately uneven so the loss differential varies; a constant differential has zero
#: variance and correctly yields no test statistic at all.
_MODEL_ERRORS = [3.0, -5.0, 7.0, -1.0, 2.0, -8.0, 4.0, 6.0]
_BASELINE_ERRORS = [4.0, -9.0, 8.0, -1.5, 6.0, -2.0, 9.0, 7.5]


def test_absolute_loss_reproduces_mae():
    result = diebold_mariano(_MODEL_ERRORS, _BASELINE_ERRORS, loss=Loss.ABSOLUTE)

    assert result.model_loss == pytest.approx(
        sum(abs(e) for e in _MODEL_ERRORS) / len(_MODEL_ERRORS)
    )
    assert result.baseline_loss == pytest.approx(
        sum(abs(e) for e in _BASELINE_ERRORS) / len(_BASELINE_ERRORS)
    )
    assert result.mean_differential == pytest.approx(
        result.model_loss - result.baseline_loss
    )


def test_squared_loss_reproduces_mean_squared_error():
    result = diebold_mariano(_MODEL_ERRORS, _BASELINE_ERRORS, loss=Loss.SQUARED)

    assert result.model_loss == pytest.approx(
        sum(e * e for e in _MODEL_ERRORS) / len(_MODEL_ERRORS)
    )


def test_a_constant_loss_differential_has_no_test_statistic():
    """Every origin differing by the same amount leaves zero variance to test against."""
    model = [3.0, -5.0, 7.0, -1.0, 2.0, -8.0, 4.0, 6.0]
    shifted = [error + (1.0 if error > 0 else -1.0) for error in model]

    with pytest.raises(InsufficientDataError, match="identical losses"):
        diebold_mariano(model, shifted)


def test_the_two_losses_can_disagree():
    """Precisely the situation in this project's headline scorecard."""
    baseline = [10.0] * 20
    model = [1.0] * 19 + [60.0]

    absolute = diebold_mariano(model, baseline, loss=Loss.ABSOLUTE)
    squared = diebold_mariano(model, baseline, loss=Loss.SQUARED)

    assert absolute.model_is_better, "many small errors beat consistent moderate ones"
    assert not squared.model_is_better, "one huge error dominates squared loss"


# -- guards --------------------------------------------------------------------


def test_unequal_lengths_are_refused():
    with pytest.raises(ValueError, match="equal lengths"):
        diebold_mariano([1.0] * 10, [1.0] * 9)


def test_too_few_pairs_are_refused():
    with pytest.raises(InsufficientDataError, match=str(MIN_PAIRS)):
        diebold_mariano([1.0, 2.0, 3.0], [2.0, 3.0, 4.0])


def test_multi_step_horizons_are_refused_rather_than_answered_wrongly():
    """Longer horizons autocorrelate the loss differential and need a HAC variance."""
    with pytest.raises(ValueError, match="one-step-ahead"):
        diebold_mariano([1.0] * 20, [2.0] * 20, horizon=2)


def test_result_carries_the_sample_size_and_degrees_of_freedom():
    result = diebold_mariano([RNG.gauss(0, 5) for _ in range(30)], [1.0] * 30)

    assert isinstance(result, ComparisonResult)
    assert result.n == 30
    assert result.degrees_of_freedom == 29


def test_small_sample_correction_shrinks_the_statistic():
    """Harvey-Leybourne-Newbold: without it the test over-rejects at n=39."""
    model = [RNG.gauss(0, 30) for _ in range(39)]
    baseline = [error + RNG.gauss(2, 30) for error in model]

    result = diebold_mariano(model, baseline)
    uncorrected = result.statistic / math.sqrt((result.n - 1) / result.n)

    assert abs(result.statistic) < abs(uncorrected)
