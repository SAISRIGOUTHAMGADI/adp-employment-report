"""Unit tests for the hand-rolled ridge fit.

Rolling our own linear algebra means owning its correctness, so these test the two
classic ridge bugs directly — a penalised intercept and a leaked scaler — plus the
additive identity the explanation layer depends on.
"""

from __future__ import annotations

import numpy as np
import pytest

from adp_forecast.forecast.ridge import (
    DEFAULT_ALPHAS,
    RidgeFit,
    _forward_chaining_splits,
)

RNG = np.random.default_rng(20260730)


def synthetic(n: int = 80, p: int = 4, noise: float = 1.0):
    """A well-conditioned linear system with known coefficients."""
    x = RNG.normal(size=(n, p)) * np.array([1.0, 10.0, 100.0, 0.1])
    true_w = np.array([2.0, -1.0, 0.5, -3.0])
    y = x @ true_w + 50.0 + RNG.normal(scale=noise, size=n)
    return x, y


# -- the algebra ---------------------------------------------------------------


def test_alpha_zero_reproduces_ordinary_least_squares():
    """The strongest correctness check available: at alpha=0 ridge *is* OLS."""
    x, y = synthetic(noise=0.5)
    fit = RidgeFit(x, y, alpha=0.0)

    design = np.column_stack([np.ones(len(x)), x])
    ols, *_ = np.linalg.lstsq(design, y, rcond=None)
    expected = design @ ols

    assert fit.predict(x).ravel() == pytest.approx(expected, rel=1e-8)


def test_predictions_are_invariant_to_feature_rescaling():
    """Standardisation must make the fit independent of the units each column arrives in."""
    x, y = synthetic()
    rescaled = x * np.array([1.0, 1000.0, 0.001, 50.0])

    base = RidgeFit(x, y, alpha=1.0).predict(x).ravel()
    scaled = RidgeFit(rescaled, y, alpha=1.0).predict(rescaled).ravel()

    assert base == pytest.approx(scaled, rel=1e-9)


def test_larger_alpha_shrinks_coefficients_monotonically():
    x, y = synthetic()
    norms = [
        float(np.linalg.norm(RidgeFit(x, y, alpha=alpha).coefficients_))
        for alpha in (0.1, 1.0, 10.0, 100.0, 1000.0)
    ]

    assert norms == sorted(norms, reverse=True)


def test_recovers_known_coefficients_with_light_penalty():
    x, y = synthetic(n=500, noise=0.1)
    fit = RidgeFit(x, y, alpha=0.01)

    # Coefficients are on standardised inputs; rescale back to raw units to compare.
    raw = fit.coefficients_ / fit.scale_
    assert raw == pytest.approx(np.array([2.0, -1.0, 0.5, -3.0]), rel=0.05)


# -- the intercept trap --------------------------------------------------------


def test_intercept_is_the_training_mean():
    x, y = synthetic()
    fit = RidgeFit(x, y, alpha=1.0)

    assert fit.intercept_ == pytest.approx(float(y.mean()))


def test_intercept_survives_an_enormous_penalty():
    """A penalised intercept would shrink toward zero and bias every forecast."""
    x, y = synthetic()
    y = y + 500.0  # far from zero, so shrinkage would be obvious

    fit = RidgeFit(x, y, alpha=1e12)

    assert np.allclose(fit.coefficients_, 0.0, atol=1e-6), "slopes must shrink"
    assert fit.intercept_ == pytest.approx(float(y.mean())), "intercept must not"
    assert float(fit.predict(x).mean()) == pytest.approx(float(y.mean()))


def test_infinite_penalty_predicts_the_mean_everywhere():
    x, y = synthetic()
    fit = RidgeFit(x, y, alpha=1e12)

    predictions = fit.predict(x).ravel()
    assert predictions == pytest.approx(np.full(len(y), y.mean()), abs=1e-4)


# -- contributions -------------------------------------------------------------


def test_contributions_sum_to_the_prediction():
    """The identity the explanation layer relies on."""
    x, y = synthetic()
    fit = RidgeFit(x, y, alpha=1.0)
    row = x[0]

    total = fit.intercept_ + float(fit.contributions(row).sum())

    assert total == pytest.approx(float(fit.predict(row)[0]), rel=1e-12)


def test_contributions_are_zero_at_the_training_mean():
    """A row exactly at the mean has nothing to explain; the forecast is the intercept."""
    x, y = synthetic()
    fit = RidgeFit(x, y, alpha=1.0)

    contributions = fit.contributions(x.mean(axis=0))

    assert contributions == pytest.approx(np.zeros(x.shape[1]), abs=1e-9)
    assert float(fit.predict(x.mean(axis=0))[0]) == pytest.approx(fit.intercept_)


def test_contribution_sign_follows_coefficient_and_deviation():
    x, y = synthetic()
    fit = RidgeFit(x, y, alpha=1.0)
    row = x.mean(axis=0).copy()
    row[0] += 5 * x[:, 0].std()

    contributions = fit.contributions(row)

    assert np.sign(contributions[0]) == np.sign(fit.coefficients_[0])
    assert contributions[1:] == pytest.approx(np.zeros(3), abs=1e-9)


# -- degenerate inputs ---------------------------------------------------------


def test_constant_column_does_not_divide_by_zero():
    x, y = synthetic()
    x[:, 2] = 7.0

    fit = RidgeFit(x, y, alpha=1.0)

    assert np.all(np.isfinite(fit.coefficients_))
    assert np.all(np.isfinite(fit.predict(x)))


def test_more_features_than_samples_still_solves():
    """Where OLS is singular, the ridge penalty makes the system solvable."""
    x = RNG.normal(size=(8, 20))
    y = RNG.normal(size=8)

    fit = RidgeFit(x, y, alpha=1.0)

    assert np.all(np.isfinite(fit.coefficients_))


def test_negative_alpha_is_rejected():
    x, y = synthetic()
    with pytest.raises(ValueError, match="non-negative"):
        RidgeFit(x, y, alpha=-1.0)


def test_shape_mismatch_is_rejected():
    x, y = synthetic()
    with pytest.raises(ValueError, match="shape mismatch"):
        RidgeFit(x, y[:-1], alpha=1.0)


def test_predict_accepts_a_single_row_or_a_matrix():
    x, y = synthetic()
    fit = RidgeFit(x, y, alpha=1.0)

    assert fit.predict(x[0]).shape == (1,)
    assert fit.predict(x).shape == (len(x),)


# -- forward-chaining cross-validation -----------------------------------------


def test_splits_never_train_on_the_future():
    """A random k-fold would, which is why this is hand-rolled rather than borrowed."""
    splits = _forward_chaining_splits(n_samples=100, folds=5, min_train=24)

    assert splits
    for train, valid in splits:
        assert train.max() < valid.min(), "training must precede validation"


def test_splits_are_disjoint_and_cover_the_tail():
    splits = _forward_chaining_splits(n_samples=100, folds=5, min_train=24)
    covered = np.concatenate([valid for _train, valid in splits])

    assert len(covered) == len(set(covered.tolist())), "no sample validated twice"
    assert covered.min() == 24
    assert covered.max() == 99


def test_training_windows_expand():
    splits = _forward_chaining_splits(n_samples=100, folds=5, min_train=24)
    sizes = [len(train) for train, _valid in splits]

    assert sizes == sorted(sizes)
    assert len(set(sizes)) > 1


def test_too_little_history_yields_no_splits():
    assert _forward_chaining_splits(n_samples=20, folds=5, min_train=24) == []
    assert _forward_chaining_splits(n_samples=25, folds=5, min_train=24) == []


def test_default_alphas_span_orders_of_magnitude():
    assert DEFAULT_ALPHAS == tuple(sorted(DEFAULT_ALPHAS))
    assert DEFAULT_ALPHAS[-1] / DEFAULT_ALPHAS[0] >= 1000
