"""Testing whether one model is genuinely more accurate than another.

A backtest that reports "MAE 62.1 versus 63.4" invites a conclusion it cannot support.
With 39 origins and per-origin errors whose standard deviation is around 88k, a gap of
1.3k is well inside the noise — but "well inside the noise" is an assertion until it is
measured. This module measures it.

Why Diebold-Mariano
-------------------
The comparison is *paired*: both models forecast the same months from the same data, so
their errors are strongly correlated and an unpaired comparison of two MAE figures throws
away exactly the information that makes the test powerful. Diebold-Mariano is the
standard test for this in the forecasting literature. It works on the **loss
differential** ``d_t = L(model error) - L(baseline error)`` and asks whether its mean is
distinguishable from zero.

Loss is parameterised because MAE and RMSE can disagree — and in this project they do,
with ridge winning on one and losing on the other. Testing absolute loss speaks to the
MAE ranking; squared loss speaks to RMSE.

Small samples
-------------
The Harvey-Leybourne-Newbold correction is applied and the statistic is compared against
Student's *t* rather than the normal. At n=39 the uncorrected test over-rejects, which
would manufacture exactly the false confidence this module exists to prevent.

Only one-step-ahead forecasts are produced here, so the loss differential carries no
autocorrelation by construction and its long-run variance is the sample variance. Longer
horizons would need a HAC estimator; :func:`diebold_mariano` rejects ``horizon > 1``
rather than silently returning an overconfident number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Final, Sequence

from ..exceptions import InsufficientDataError
from ..logging_config import get_logger

_LOG = get_logger(__name__)

#: Fewest paired observations worth testing. Below this the test has almost no power and
#: a non-rejection says nothing at all.
MIN_PAIRS: Final[int] = 8

#: Conventional significance threshold used for the plain-language verdict.
ALPHA: Final[float] = 0.05


class Loss(str, Enum):
    """Which loss function the comparison is made under.

    ``ABSOLUTE`` corresponds to MAE, ``SQUARED`` to RMSE. Reported separately because a
    model can win under one and lose under the other, and collapsing them would hide it.
    """

    ABSOLUTE = "absolute"
    SQUARED = "squared"


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Outcome of a paired accuracy comparison.

    Attributes:
        model: Model under test.
        baseline: Model compared against.
        loss: Loss function used.
        n: Paired observations.
        model_loss: Mean loss for the model. MAE when ``loss`` is absolute.
        baseline_loss: Mean loss for the baseline.
        mean_differential: ``model_loss - baseline_loss``. Negative favours the model.
        statistic: HLN-corrected Diebold-Mariano statistic.
        p_value: Two-sided p-value under Student's *t* with ``n - 1`` degrees of freedom.
        degrees_of_freedom: ``n - 1``.
    """

    model: str
    baseline: str
    loss: Loss
    n: int
    model_loss: float
    baseline_loss: float
    mean_differential: float
    statistic: float
    p_value: float
    degrees_of_freedom: int

    @property
    def model_is_better(self) -> bool:
        """Whether the model has lower mean loss, regardless of significance."""
        return self.mean_differential < 0.0

    @property
    def is_significant(self) -> bool:
        """Whether the difference is distinguishable from zero at :data:`ALPHA`."""
        return self.p_value < ALPHA

    @property
    def verdict(self) -> str:
        """One-line plain-language reading of the result.

        Deliberately refuses to describe a non-significant difference as a win. The point
        of running the test is to stop a 2% gap on 39 observations being reported as an
        improvement.
        """
        direction = "lower" if self.model_is_better else "higher"
        if self.is_significant:
            return (
                f"{self.model} has significantly {direction} "
                f"{self.loss.value} loss than {self.baseline} (p={self.p_value:.3f})"
            )
        return (
            f"{self.model} and {self.baseline} are statistically indistinguishable "
            f"under {self.loss.value} loss (p={self.p_value:.3f})"
        )


def diebold_mariano(
    model_errors: Sequence[float],
    baseline_errors: Sequence[float],
    *,
    model_name: str = "model",
    baseline_name: str = "baseline",
    loss: Loss = Loss.ABSOLUTE,
    horizon: int = 1,
) -> ComparisonResult:
    """Test whether two sets of paired forecast errors differ in accuracy.

    Args:
        model_errors: Per-origin errors (``forecast - actual``) for the model.
        baseline_errors: Errors for the baseline, aligned origin-for-origin.
        model_name: Label for the model.
        baseline_name: Label for the baseline.
        loss: Loss function.
        horizon: Forecast horizon. Only ``1`` is supported.

    Returns:
        A populated :class:`ComparisonResult`.

    Raises:
        ValueError: If the error sequences differ in length, or ``horizon`` is not 1.
        InsufficientDataError: If there are fewer than :data:`MIN_PAIRS` pairs, or the
            loss differential has zero variance so no test statistic exists.
    """
    if len(model_errors) != len(baseline_errors):
        raise ValueError(
            f"Paired test needs equal lengths, got {len(model_errors)} and "
            f"{len(baseline_errors)}. Score both models on the same origins first."
        )
    if horizon != 1:
        raise ValueError(
            f"Only one-step-ahead comparison is supported, got horizon={horizon}. "
            "Longer horizons induce autocorrelation in the loss differential and need "
            "a HAC variance estimator."
        )

    n = len(model_errors)
    if n < MIN_PAIRS:
        raise InsufficientDataError(
            f"Need at least {MIN_PAIRS} paired forecasts to test accuracy, got {n}."
        )

    apply_loss = _LOSS_FUNCTIONS[loss]
    model_losses = [apply_loss(error) for error in model_errors]
    baseline_losses = [apply_loss(error) for error in baseline_errors]
    differentials = [
        model - baseline for model, baseline in zip(model_losses, baseline_losses)
    ]

    mean_differential = fmean(differentials)
    # Sample variance of the differential. For h=1 this is the long-run variance, since
    # one-step-ahead loss differentials are serially uncorrelated under the null.
    variance = sum((d - mean_differential) ** 2 for d in differentials) / (n - 1)
    if variance <= 0.0:
        raise InsufficientDataError(
            f"{model_name} and {baseline_name} produced identical losses at every "
            "origin, so no test statistic is defined."
        )

    statistic = mean_differential / math.sqrt(variance / n)

    # Harvey-Leybourne-Newbold small-sample correction. At h=1 the factor reduces to
    # sqrt((n - 1) / n); without it the test over-rejects at this sample size.
    correction = math.sqrt((n + 1 - 2 * horizon) / n)
    corrected = statistic * correction

    p_value = _student_t_two_sided_p(corrected, degrees_of_freedom=n - 1)

    result = ComparisonResult(
        model=model_name,
        baseline=baseline_name,
        loss=loss,
        n=n,
        model_loss=fmean(model_losses),
        baseline_loss=fmean(baseline_losses),
        mean_differential=mean_differential,
        statistic=corrected,
        p_value=p_value,
        degrees_of_freedom=n - 1,
    )
    _LOG.debug("%s", result.verdict)
    return result


_LOSS_FUNCTIONS: Final[dict[Loss, "object"]] = {
    Loss.ABSOLUTE: abs,
    Loss.SQUARED: lambda error: error * error,
}


# ---------------------------------------------------------------------------
# Student's t distribution
# ---------------------------------------------------------------------------
# Implemented rather than taken from scipy, which would be a ~40 MB dependency for one
# function in a project that otherwise installs in seconds. The regularised incomplete
# beta below is standard numerical code and is tested against published critical values.


def _student_t_two_sided_p(statistic: float, *, degrees_of_freedom: int) -> float:
    """Two-sided p-value for a *t* statistic.

    Uses the identity ``P(|T| > t) = I_{df/(df+t^2)}(df/2, 1/2)``.

    Args:
        statistic: The t statistic.
        degrees_of_freedom: Degrees of freedom, at least 1.
    """
    if degrees_of_freedom < 1:
        raise ValueError(f"degrees_of_freedom must be >= 1, got {degrees_of_freedom}")
    if statistic == 0.0:
        return 1.0

    x = degrees_of_freedom / (degrees_of_freedom + statistic * statistic)
    return _regularised_incomplete_beta(degrees_of_freedom / 2.0, 0.5, x)


def _regularised_incomplete_beta(a: float, b: float, x: float) -> float:
    """Return the regularised incomplete beta function ``I_x(a, b)``.

    Continued-fraction evaluation via Lentz's algorithm, with the standard reflection
    ``I_x(a,b) = 1 - I_{1-x}(b,a)`` applied where the fraction converges slowly.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    log_prefactor = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    prefactor = math.exp(log_prefactor)

    if x < (a + 1.0) / (a + b + 2.0):
        return prefactor * _beta_continued_fraction(a, b, x) / a
    return 1.0 - prefactor * _beta_continued_fraction(b, a, 1.0 - x) / b


def _beta_continued_fraction(
    a: float,
    b: float,
    x: float,
    *,
    max_iterations: int = 300,
    epsilon: float = 3e-16,
) -> float:
    """Evaluate the continued fraction for the incomplete beta function.

    Modified Lentz's method. ``tiny`` guards against a zero denominator, which the
    algorithm can otherwise produce mid-iteration.
    """
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    result = d

    for m in range(1, max_iterations + 1):
        m2 = 2 * m

        # Even step.
        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        result *= d * c

        # Odd step.
        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        result *= delta

        if abs(delta - 1.0) < epsilon:
            return result

    _LOG.warning(
        "Incomplete beta did not converge in %d iterations (a=%g, b=%g, x=%g)",
        max_iterations,
        a,
        b,
        x,
    )
    return result
