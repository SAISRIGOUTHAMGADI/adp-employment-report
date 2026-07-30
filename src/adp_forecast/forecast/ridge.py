"""Ridge regression, hand-rolled on numpy.

Chosen over scikit-learn on purpose. The fit is a closed form,

.. math:: w = (X^\\top X + \\lambda I)^{-1} X^\\top y

which at 168 rows and 9 columns is exact and instant, and it adds no dependency to a
clone-and-run (``numpy`` already arrives with ``pandas``). The decisive reason, though,
is the explanation requirement: a linear model's prediction decomposes *additively* into
``coefficient x feature``, so "why this number" is arithmetic that provably sums to the
forecast rather than a story told about a black box. :meth:`RidgeForecaster.forecast`
asserts that identity on every call.

The two classic ridge bugs, and how each is prevented
-----------------------------------------------------
1. **Penalising the intercept.** :math:`\\lambda` must shrink slopes, not the mean of
   the target. Here ``y`` is centred and ``X`` standardised before solving, so no
   intercept column enters the penalised system at all — the intercept is recovered
   afterwards as the training mean. It is unpenalised by construction, not by a flag.
2. **Fitting the scaler on data the model should not see.** Standardisation statistics
   come from the training rows of the current fit only. Because models here are
   stateless and refit per origin, a walk-forward backtest cannot reuse statistics
   computed from a later window.

Regularisation strength is chosen by forward-chaining cross-validation — never a random
k-fold, which would train on months after the ones it validates on.
"""

from __future__ import annotations

from typing import Final, Sequence

import numpy as np

from ..config import TARGET_SERIES_ID
from ..units import canonical_unit_label
from ..features import FeaturePanel
from ..logging_config import get_logger
from .baselines import DEFAULT_INTERVAL_LEVEL, usable_changes
from .design import DEFAULT_TERMS, DesignMatrix, FeatureTerm, build_design_matrix
from .port import Driver, Forecast

_LOG = get_logger(__name__)

#: Candidate penalties, geometric so the search spans four orders of magnitude cheaply.
DEFAULT_ALPHAS: Final[tuple[float, ...]] = (
    0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0,
)

#: Forward-chaining CV folds used to select the penalty.
_CV_FOLDS: Final[int] = 5

#: Smallest training block a CV fold may validate against.
_CV_MIN_TRAIN: Final[int] = 24

#: Guards against dividing by the standard deviation of a constant column.
_STD_FLOOR: Final[float] = 1e-12


class RidgeFit:
    """A fitted ridge model: standardisation statistics plus coefficients.

    Separated from the forecaster so the linear algebra can be unit-tested directly,
    including against ordinary least squares at ``alpha=0``.
    """

    def __init__(self, x: np.ndarray, y: np.ndarray, alpha: float) -> None:
        """Fit by the closed-form solution on standardised inputs.

        Args:
            x: Training features, shape ``(n_samples, n_terms)``.
            y: Training targets, shape ``(n_samples,)``.
            alpha: Ridge penalty. ``0`` reduces exactly to OLS.

        Raises:
            ValueError: If ``alpha`` is negative or the shapes disagree.
        """
        if alpha < 0:
            raise ValueError(f"alpha must be non-negative, got {alpha}")
        if x.ndim != 2 or y.ndim != 1 or x.shape[0] != y.shape[0]:
            raise ValueError(f"shape mismatch: x={x.shape}, y={y.shape}")

        self.alpha = alpha
        self.mean_ = x.mean(axis=0)
        # ddof=0: these are the statistics of this training set, not an estimate of a
        # wider population.
        self.scale_ = np.maximum(x.std(axis=0), _STD_FLOOR)
        self.y_mean_ = float(y.mean())

        x_std = (x - self.mean_) / self.scale_
        y_centred = y - self.y_mean_

        n_terms = x_std.shape[1]
        gram = x_std.T @ x_std + alpha * np.eye(n_terms)
        # solve() rather than inv(): numerically stabler and never forms the inverse.
        self.coefficients_ = np.linalg.solve(gram, x_std.T @ y_centred)

    @property
    def intercept_(self) -> float:
        """The unpenalised intercept: the training mean of the target."""
        return self.y_mean_

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Predict for one or more rows.

        Args:
            x: Features, shape ``(n_terms,)`` or ``(n_samples, n_terms)``.
        """
        rows = np.atleast_2d(x)
        standardised = (rows - self.mean_) / self.scale_
        return standardised @ self.coefficients_ + self.y_mean_

    def contributions(self, x: np.ndarray) -> np.ndarray:
        """Per-term contribution for a single row, in target units.

        These sum to ``predict(x) - intercept_`` exactly, which is what makes the
        explanation defensible rather than indicative.

        Args:
            x: A single feature row, shape ``(n_terms,)``.
        """
        standardised = (np.asarray(x) - self.mean_) / self.scale_
        return standardised * self.coefficients_


class RidgeForecaster:
    """Ridge regression over the declared feature terms.

    Stateless: every :meth:`forecast` call refits from the supplied panel, so a
    walk-forward backtest cannot reuse a fit from a later origin.
    """

    def __init__(
        self,
        terms: Sequence[FeatureTerm] = DEFAULT_TERMS,
        *,
        alphas: Sequence[float] = DEFAULT_ALPHAS,
        interval_level: float = DEFAULT_INTERVAL_LEVEL,
        min_samples: int = 36,
    ) -> None:
        """Configure the model.

        Args:
            terms: Design-matrix columns.
            alphas: Candidate penalties for cross-validation.
            interval_level: Nominal coverage of the reported interval.
            min_samples: Fewest training rows worth fitting on.

        Raises:
            ValueError: If ``alphas`` is empty or ``interval_level`` is not in (0, 1).
        """
        if not alphas:
            raise ValueError("alphas must not be empty")
        if not 0.0 < interval_level < 1.0:
            raise ValueError(
                f"interval_level must be between 0 and 1, got {interval_level}"
            )
        self._terms = tuple(terms)
        self._alphas = tuple(alphas)
        self._interval_level = interval_level
        self._min_samples = min_samples

    @property
    def name(self) -> str:
        """Model identifier."""
        return "ridge"

    def forecast(self, panel: FeaturePanel) -> Forecast:
        """Fit on the panel's history and predict its target month.

        See :meth:`~adp_forecast.forecast.port.ForecastPort.forecast`.
        """
        design = build_design_matrix(
            panel, self._terms, min_samples=self._min_samples
        )
        alpha = self._select_alpha(design)
        fit = RidgeFit(design.x, design.y, alpha)

        point = float(fit.predict(design.x_next)[0])
        contributions = fit.contributions(design.x_next)

        # The additive identity the explanation layer relies on. If this ever fails the
        # explanation would be describing a different number than the one reported.
        reconstructed = fit.intercept_ + float(contributions.sum())
        if not np.isclose(reconstructed, point, rtol=1e-9, atol=1e-9):
            raise AssertionError(
                "Ridge contributions do not reconstruct the prediction: "
                f"{reconstructed} vs {point}"
            )

        drivers = self._build_drivers(design, fit, contributions)
        lower, upper = self._interval(design, alpha, point)

        _LOG.info(
            "Ridge forecast for %s: %+.1fk (alpha=%g, n=%d)",
            design.target_month.isoformat(),
            point,
            alpha,
            design.n_samples,
        )
        history = usable_changes(panel)
        return Forecast(
            series_id=TARGET_SERIES_ID,
            month=design.target_month,
            as_of=panel.as_of,
            point=point,
            lower=lower,
            upper=upper,
            interval_level=self._interval_level,
            model_name=self.name,
            drivers=drivers,
            n_train=design.n_samples,
            baseline_point=history[-1].change if history else None,
        )

    # -- internals ---------------------------------------------------------

    def _build_drivers(
        self,
        design: DesignMatrix,
        fit: RidgeFit,
        contributions: np.ndarray,
    ) -> tuple[Driver, ...]:
        """Package per-term contributions, largest absolute effect first."""
        drivers = [
            Driver(
                name=term.name,
                label=term.label,
                value=float(design.x_next[index]),
                contribution=float(contributions[index]),
                coefficient=float(fit.coefficients_[index]),
                unit_label=canonical_unit_label(term.series_id),
            )
            for index, term in enumerate(design.terms)
        ]
        drivers.sort(key=lambda driver: abs(driver.contribution), reverse=True)
        return tuple(drivers)

    def _select_alpha(self, design: DesignMatrix) -> float:
        """Choose the penalty by forward-chaining cross-validation.

        Each fold trains on an expanding prefix and validates on the block immediately
        after it, so no fold ever trains on months later than the ones it scores. A
        random k-fold would do exactly that and would flatter the model.

        Falls back to the middle candidate when there is too little history to split,
        rather than silently using an unvalidated extreme.
        """
        if len(self._alphas) == 1:
            return self._alphas[0]

        splits = _forward_chaining_splits(design.n_samples, _CV_FOLDS, _CV_MIN_TRAIN)
        if not splits:
            fallback = self._alphas[len(self._alphas) // 2]
            _LOG.debug(
                "Too little history (%d rows) to cross-validate; using alpha=%g",
                design.n_samples,
                fallback,
            )
            return fallback

        best_alpha = self._alphas[0]
        best_error = float("inf")
        for alpha in self._alphas:
            errors = [
                design.y[valid] - RidgeFit(
                    design.x[train], design.y[train], alpha
                ).predict(design.x[valid])
                for train, valid in splits
            ]
            mean_squared = float(np.mean(np.concatenate(errors) ** 2))
            if mean_squared < best_error:
                best_error, best_alpha = mean_squared, alpha

        _LOG.debug("Selected alpha=%g (CV MSE %.1f)", best_alpha, best_error)
        return best_alpha

    def _interval(
        self,
        design: DesignMatrix,
        alpha: float,
        point: float,
    ) -> tuple[float | None, float | None]:
        """Build an interval from empirical quantiles of out-of-sample residuals.

        Quantiles of realised forward-chaining errors, not model-implied variance.
        Payroll forecast errors are not reliably normal, and a variance-based interval
        would encode an assumption the data does not support. Empirical quantiles make
        no distributional claim about *shape* — they report what this model's errors
        actually did.

        Known limitation: non-stationary error dispersion
        ------------------------------------------------
        The method does still assume error *dispersion* is stable between the training
        history and the month being forecast, and measurement says it is not. Comparing
        the residual pool against realised backtest error:

        ===========  ==================  ===============  ======  ========
        Scorecard    realised error sd   residual pool sd  ratio   coverage
        ===========  ==================  ===============  ======  ========
        vintage           87.9k              118.3k        0.74      85%
        lag_shifted       64.7k               53.4k        1.21      71%
        ===========  ==================  ===============  ======  ========

        Interval width tracks the residual pool, so where the pool is wider than
        realised error the interval over-covers, and where it is narrower it
        under-covers. The two scorecards miss in opposite directions, which rules out a
        constant correction factor.

        A hypothesis that alpha selection was double-dipping — choosing the penalty by
        minimising error over the same folds whose residuals build the interval — was
        tested and **refuted**: pinning alpha moved coverage by under one point.

        Left uncorrected deliberately. Fitting a residual window or a scale factor by
        watching backtest coverage is the same test-set tuning refused for the training
        window, and would make the reported coverage meaningless. The headline
        (vintage) scorecard errs conservative at 85% against a nominal 80%, which is the
        safe direction. The under-covering case is confined to the approximate
        scorecard, and is reported rather than papered over.

        Returns ``(None, None)`` when there are too few residuals for the requested
        quantiles to mean anything.
        """
        splits = _forward_chaining_splits(design.n_samples, _CV_FOLDS, _CV_MIN_TRAIN)
        if not splits:
            return None, None

        residuals = np.concatenate(
            [
                design.y[valid] - RidgeFit(
                    design.x[train], design.y[train], alpha
                ).predict(design.x[valid])
                for train, valid in splits
            ]
        )
        if residuals.size < 10:
            _LOG.debug(
                "Only %d residual(s); omitting interval rather than implying precision",
                residuals.size,
            )
            return None, None

        tail = (1.0 - self._interval_level) / 2.0
        lower_q, upper_q = np.quantile(residuals, [tail, 1.0 - tail])
        return point + float(lower_q), point + float(upper_q)


def _forward_chaining_splits(
    n_samples: int,
    folds: int,
    min_train: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Build expanding-window train/validation index pairs.

    Fold *k* trains on everything before a cut point and validates on the block that
    follows, with the cut point advancing. Respects time order, which random k-fold
    does not.

    Args:
        n_samples: Rows available.
        folds: Desired number of folds.
        min_train: Smallest training prefix allowed.

    Returns:
        ``(train_index, validation_index)`` pairs, empty if the data cannot support
        even one fold.
    """
    if n_samples <= min_train + 1:
        return []

    validation_total = n_samples - min_train
    block = max(1, validation_total // folds)
    splits: list[tuple[np.ndarray, np.ndarray]] = []

    start = min_train
    while start < n_samples:
        stop = min(start + block, n_samples)
        splits.append((np.arange(0, start), np.arange(start, stop)))
        start = stop
    return splits
