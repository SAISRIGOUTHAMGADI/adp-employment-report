"""Forecast error metrics.

Metric choice is a design decision here, not a formality, so the reasoning is recorded
alongside each one.

**MAE is primary.** It is in the same units as the forecast — thousands of jobs — so
"we are typically 62k out" is directly interpretable against a print that itself runs
around 100k.

**RMSE is secondary** and reported always, because it penalises large misses
quadratically and can disagree with MAE. A model that wins on MAE while losing on RMSE
is trading many small errors for a few large ones, and a reader deserves to see that
rather than have it averaged away.

**MAPE is deliberately absent.** The target changes sign and passes near zero: the ADP
print has been -1k, +11k and +22k in recent history. Percentage error against a near-zero
denominator explodes without bound, so MAPE would rank models by how well they avoided
small-actual months rather than by accuracy. It is not a conservative choice here, it is
a broken one.

**MASE is not used either.** It scales error by a naive baseline's error, which is
useful when comparing across series. Here there is one series and the baselines are
reported directly, so a ratio would hide the absolute magnitude that actually matters.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Sequence

from ..exceptions import InsufficientDataError


@dataclass(frozen=True, slots=True)
class ScoreCard:
    """Aggregate accuracy for one model over one set of origins.

    Attributes:
        model_name: Model scored.
        n: Forecasts scored.
        mae: Mean absolute error, thousands of jobs. The primary metric.
        rmse: Root mean squared error, thousands of jobs.
        bias: Mean signed error. Positive means the model forecasts too high.
        directional_accuracy: Share of months where the sign of the forecast change
            matched the sign of the actual, or ``None`` when no month had a non-zero
            actual to judge against.
        interval_coverage: Share of actuals falling inside the prediction interval,
            or ``None`` if no forecast carried one.
        mean_interval_width: Mean width of the intervals, or ``None``.
        n_with_interval: How many forecasts carried an interval.
    """

    model_name: str
    n: int
    mae: float
    rmse: float
    bias: float
    directional_accuracy: float | None
    interval_coverage: float | None
    mean_interval_width: float | None
    n_with_interval: int

    def coverage_gap(self, nominal: float) -> float | None:
        """Signed difference between realised and nominal coverage.

        Negative means the interval is too narrow — it covers less often than it
        claims, which is the failure mode that matters. An interval that under-covers
        is worse than no interval, because it advertises a precision it does not have.

        Args:
            nominal: The interval's nominal coverage, e.g. ``0.80``.
        """
        if self.interval_coverage is None:
            return None
        return self.interval_coverage - nominal


def mean_absolute_error(errors: Sequence[float]) -> float:
    """Mean absolute error. ``error = forecast - actual``."""
    return fmean(abs(error) for error in errors)


def root_mean_squared_error(errors: Sequence[float]) -> float:
    """Root mean squared error."""
    return fmean(error * error for error in errors) ** 0.5


def mean_error(errors: Sequence[float]) -> float:
    """Mean signed error. Positive means forecasting too high."""
    return fmean(errors)


def directional_accuracy(
    forecasts: Sequence[float],
    actuals: Sequence[float],
) -> float | None:
    """Share of months where forecast and actual moved the same way.

    Months with an actual of exactly zero are skipped: there is no direction to get
    right, and counting them either way would distort the measure.

    Args:
        forecasts: Predicted changes.
        actuals: Realised changes.

    Returns:
        A share in ``[0, 1]``, or ``None`` if no month had a non-zero actual.
    """
    judged = [
        (forecast, actual)
        for forecast, actual in zip(forecasts, actuals)
        if actual != 0.0
    ]
    if not judged:
        return None
    hits = sum(
        1 for forecast, actual in judged if (forecast >= 0.0) == (actual >= 0.0)
    )
    return hits / len(judged)


def interval_coverage(
    actuals: Sequence[float],
    lowers: Sequence[float | None],
    uppers: Sequence[float | None],
) -> tuple[float | None, float | None, int]:
    """Realised coverage and mean width of prediction intervals.

    Compares realised coverage against what the interval claims. Under-coverage is a
    defect: an 80% interval that contains the truth 72% of the time is asserting a
    precision the model does not have.

    Args:
        actuals: Realised values.
        lowers: Lower bounds, ``None`` where absent.
        uppers: Upper bounds, ``None`` where absent.

    Returns:
        ``(coverage, mean_width, n_with_interval)``. Coverage and width are ``None``
        when no forecast carried an interval.
    """
    pairs = [
        (actual, lower, upper)
        for actual, lower, upper in zip(actuals, lowers, uppers)
        if lower is not None and upper is not None
    ]
    if not pairs:
        return None, None, 0

    inside = sum(1 for actual, lower, upper in pairs if lower <= actual <= upper)
    width = fmean(upper - lower for _actual, lower, upper in pairs)
    return inside / len(pairs), width, len(pairs)


def score(
    model_name: str,
    forecasts: Sequence[float],
    actuals: Sequence[float],
    lowers: Sequence[float | None] = (),
    uppers: Sequence[float | None] = (),
) -> ScoreCard:
    """Compute every metric for one model.

    Args:
        model_name: Model being scored.
        forecasts: Point forecasts.
        actuals: Realised values, aligned with ``forecasts``.
        lowers: Interval lower bounds, aligned. Omit for models without intervals.
        uppers: Interval upper bounds, aligned.

    Returns:
        A populated :class:`ScoreCard`.

    Raises:
        InsufficientDataError: If there is nothing to score.
        ValueError: If the sequences are not the same length.
    """
    if not forecasts:
        raise InsufficientDataError(f"No forecasts to score for {model_name}")
    if len(forecasts) != len(actuals):
        raise ValueError(
            f"{model_name}: {len(forecasts)} forecasts vs {len(actuals)} actuals"
        )

    errors = [forecast - actual for forecast, actual in zip(forecasts, actuals)]
    coverage, width, n_interval = interval_coverage(
        actuals,
        lowers or [None] * len(actuals),
        uppers or [None] * len(actuals),
    )

    return ScoreCard(
        model_name=model_name,
        n=len(errors),
        mae=mean_absolute_error(errors),
        rmse=root_mean_squared_error(errors),
        bias=mean_error(errors),
        directional_accuracy=directional_accuracy(forecasts, actuals),
        interval_coverage=coverage,
        mean_interval_width=width,
        n_with_interval=n_interval,
    )
