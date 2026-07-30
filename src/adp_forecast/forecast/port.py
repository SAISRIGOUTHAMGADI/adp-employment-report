"""Forecast contracts and result types.

Every model — the naive baselines and the ridge regression alike — is reached through
:class:`ForecastPort`. That is what lets the evaluation layer score them uniformly
without knowing what any of them is, and what makes adding a model an adapter rather
than a change to the backtest.

Models are **stateless**: :meth:`ForecastPort.forecast` fits and predicts in one call.
A model that carried a fitted state between calls could silently reuse a fit from a
later origin during a walk-forward backtest, which is a leak with no symptom. Refitting
per origin is also what a real forecaster does.

Results are typed objects, never formatted strings. The CLI, the eventual HTTP layer and
the explanation layer all render the same :class:`Forecast`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from ..features import FeaturePanel


@dataclass(frozen=True, slots=True)
class Driver:
    """One named contribution to a point forecast.

    The unit of explanation. A linear model's prediction decomposes exactly into an
    intercept plus one of these per feature, so the "why" is arithmetic rather than
    narration — and the contributions provably sum to the forecast, which
    :mod:`adp_forecast.forecast.ridge` asserts.

    Attributes:
        name: Machine-readable term name, e.g. ``icsa_change``.
        label: Human-readable description for output.
        value: The feature's value at forecast time, in its own units.
        contribution: Thousands of jobs this term added to (or subtracted from) the
            forecast, relative to the training mean.
        coefficient: Fitted weight on the standardised feature. Comparable across
            terms because the inputs were standardised.
        unit_label: Canonical units of ``value``, e.g. ``"thousands of persons"``.
            Carried so the explanation layer can render the number with its units
            instead of emitting a bare figure the reader has to guess at.
    """

    name: str
    label: str
    value: float
    contribution: float
    coefficient: float
    unit_label: str = ""

    @property
    def direction(self) -> str:
        """``"raises"``, ``"lowers"`` or ``"neutral"``, for prose output."""
        if self.contribution > 0.5:
            return "raises"
        if self.contribution < -0.5:
            return "lowers"
        return "neutral"


@dataclass(frozen=True, slots=True)
class Forecast:
    """A prediction for one month, with its interval and its reasoning.

    Attributes:
        series_id: Series forecast.
        month: Reference month predicted.
        as_of: Vantage date the forecast was made from. Everything used was published
            on or before it.
        point: Point forecast of the month-over-month change, in thousands of jobs.
        lower: Lower interval bound, or ``None`` if no interval was available.
        upper: Upper interval bound, or ``None``.
        interval_level: Nominal coverage of the interval, e.g. ``0.80``.
        model_name: Which model produced this.
        drivers: Per-term contributions, largest absolute first. Empty for baselines,
            which have no features to attribute to.
        n_train: Observations the model was fitted on, after regime exclusion.
        baseline_point: The random-walk forecast for the same month, carried so output
            can always show what the model is adding over doing nothing.
    """

    series_id: str
    month: date
    as_of: date
    point: float
    lower: float | None
    upper: float | None
    interval_level: float
    model_name: str
    drivers: tuple[Driver, ...]
    n_train: int
    baseline_point: float | None = None

    @property
    def has_interval(self) -> bool:
        """True when both interval bounds are present."""
        return self.lower is not None and self.upper is not None

    @property
    def interval_width(self) -> float | None:
        """Width of the interval in thousands, or ``None`` if absent."""
        if self.lower is None or self.upper is None:
            return None
        return self.upper - self.lower

    def top_drivers(self, count: int = 3) -> tuple[Driver, ...]:
        """Return the ``count`` largest contributions by absolute size.

        Args:
            count: How many to return.
        """
        return self.drivers[:count]


@runtime_checkable
class ForecastPort(Protocol):
    """A model that turns a feature panel into a forecast.

    Implementations must be stateless across calls and must not read anything outside
    the supplied panel — the panel is already constrained to one vantage date, so
    respecting it is what makes a backtest leak-free.
    """

    @property
    def name(self) -> str:
        """Short identifier used in reports and to select the model on the CLI."""
        ...

    def forecast(self, panel: FeaturePanel) -> Forecast:
        """Fit on the panel's history and predict its target month.

        Args:
            panel: Everything knowable at the forecast origin.

        Returns:
            A :class:`Forecast` for ``panel.target_month``.

        Raises:
            InsufficientDataError: If the panel holds too little usable history to
                fit, after regime exclusion.
        """
        ...
