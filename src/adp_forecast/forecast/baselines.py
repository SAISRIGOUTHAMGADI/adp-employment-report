"""Naive baselines the model has to beat.

A forecast is only interesting relative to the cheapest thing that could have been done
instead. These are those cheap things, implemented behind the same
:class:`~adp_forecast.forecast.port.ForecastPort` as the real model so the evaluator
scores them identically.

Notably absent: **seasonal naive**. ``ADPMNUSNERSA`` is already seasonally adjusted, so
predicting "same as twelve months ago" would re-apply a seasonal pattern that has been
removed. It would not be a weak baseline, it would be a wrong one.

For a seasonally adjusted monthly series the honest bar is the random walk. Beating a
3- or 6-month mean but losing to last-value would mean the model has learned the series'
average and nothing about its dynamics.
"""

from __future__ import annotations

from statistics import fmean
from typing import Final, Sequence

from ..config import TARGET_SERIES_ID, is_excluded_month
from ..domain import MonthlyChange
from ..exceptions import InsufficientDataError
from ..features import FeaturePanel
from ..logging_config import get_logger
from .port import Forecast

_LOG = get_logger(__name__)

#: Nominal coverage for baseline intervals, matching the ridge model's default.
DEFAULT_INTERVAL_LEVEL: Final[float] = 0.80

#: Multiplier turning a residual standard deviation into an 80% interval half-width
#: under a normal assumption. Baselines report intervals this way because they have no
#: backtest residuals of their own to draw empirical quantiles from; the ridge model
#: uses empirical quantiles instead, which is the better method where it is available.
_NORMAL_80_Z: Final[float] = 1.2816


def usable_changes(panel: FeaturePanel) -> list[MonthlyChange]:
    """Return the panel's target changes with the excluded regime removed.

    Shared by every baseline so they all train on exactly the same history the ridge
    model does — otherwise a baseline comparison would be measuring the regime filter
    rather than the model.

    Args:
        panel: The forecast panel.
    """
    return [
        change
        for change in panel.target_changes
        if not is_excluded_month(change.month)
    ]


class _BaselineBase:
    """Shared plumbing for the naive models.

    Subclasses implement :meth:`_predict`; this class handles history filtering, the
    interval, and packaging the :class:`Forecast`.
    """

    #: Fewest usable observations before a baseline will produce a forecast.
    min_samples: int = 3

    @property
    def name(self) -> str:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError

    def forecast(self, panel: FeaturePanel) -> Forecast:
        """Fit on the panel's usable history and predict its target month."""
        history = usable_changes(panel)
        if len(history) < self.min_samples:
            raise InsufficientDataError(
                f"{self.name} needs {self.min_samples} usable observations, "
                f"got {len(history)} for {panel.target_month}."
            )

        values = [change.change for change in history]
        point = self._predict(values)
        lower, upper = self._interval(values, point)

        return Forecast(
            series_id=TARGET_SERIES_ID,
            month=panel.target_month,
            as_of=panel.as_of,
            point=point,
            lower=lower,
            upper=upper,
            interval_level=DEFAULT_INTERVAL_LEVEL,
            model_name=self.name,
            drivers=(),
            n_train=len(values),
            baseline_point=values[-1],
        )

    def _predict(self, values: Sequence[float]) -> float:
        """Return the point forecast from the usable history, oldest first."""
        raise NotImplementedError

    def _interval(
        self, values: Sequence[float], point: float
    ) -> tuple[float | None, float | None]:
        """Return an interval from the in-sample dispersion of this rule.

        Uses the spread of the rule's own one-step errors over the history rather than
        the spread of the series, so a smoother rule gets a narrower interval.
        """
        errors = self._in_sample_errors(values)
        if len(errors) < 2:
            return None, None
        spread = (fmean(error * error for error in errors)) ** 0.5
        half_width = _NORMAL_80_Z * spread
        return point - half_width, point + half_width

    def _in_sample_errors(self, values: Sequence[float]) -> list[float]:
        """One-step-ahead errors this rule would have made over the history."""
        errors: list[float] = []
        for index in range(self.min_samples, len(values)):
            predicted = self._predict(values[:index])
            errors.append(values[index] - predicted)
        return errors


class RandomWalkForecaster(_BaselineBase):
    """Predicts that next month equals last month.

    The honest bar for a seasonally adjusted series. If the model cannot beat this, it
    has not learned anything about the dynamics.
    """

    min_samples = 2

    @property
    def name(self) -> str:
        return "random_walk"

    def _predict(self, values: Sequence[float]) -> float:
        return values[-1]


class MovingAverageForecaster(_BaselineBase):
    """Predicts the mean of the last ``window`` months.

    Smoother than the random walk, and hard to beat on a noisy series: much of the
    month-to-month movement in payroll prints is not predictable, so averaging it away
    is a genuinely strong strategy rather than a strawman.
    """

    def __init__(self, window: int = 3) -> None:
        """Configure the averaging window.

        Args:
            window: Months to average. Must be at least 1.

        Raises:
            ValueError: If ``window`` is below 1.
        """
        if window < 1:
            raise ValueError(f"window must be at least 1, got {window}")
        self._window = window
        self.min_samples = max(window, 2)

    @property
    def name(self) -> str:
        return f"mean_{self._window}m"

    @property
    def window(self) -> int:
        """Months averaged."""
        return self._window

    def _predict(self, values: Sequence[float]) -> float:
        return fmean(values[-self._window:])


class DriftForecaster(_BaselineBase):
    """Extends the average trend of the history from the last observation.

    Last value plus the mean per-period change. Included because it is the natural
    "the series is trending" baseline, and payroll growth does trend over quarters.
    """

    min_samples = 3

    @property
    def name(self) -> str:
        return "drift"

    def _predict(self, values: Sequence[float]) -> float:
        if len(values) < 2:
            return values[-1]
        drift = (values[-1] - values[0]) / (len(values) - 1)
        return values[-1] + drift
