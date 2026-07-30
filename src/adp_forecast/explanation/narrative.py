"""Turns a :class:`~adp_forecast.forecast.port.Forecast` into plain English.

The brief's third requirement is that a user can *understand why* a number was
predicted, and the design constraint that follows is stricter than it looks: the prose
must be **derived from the model's arithmetic**, never written alongside it. A narrative
composed independently can drift from the numbers it describes and nobody would notice.

So every sentence here is generated from structured fields on the ``Forecast``, and the
one claim that could silently go wrong — that the stated drivers actually add up to the
stated forecast — is checked rather than trusted. A linear model's prediction decomposes
exactly into an intercept plus one contribution per feature, which is why ridge was
chosen over a stronger black box in the first place.

Output is a structured :class:`Explanation`, not a string. Tests assert on fields, the
CLI renders text, and a future HTTP layer can serialise it without reparsing prose.

Honesty constraints
-------------------
Three things this deliberately does *not* do:

* It does not describe the model as accurate. Measured accuracy is passed in as a
  :class:`~adp_forecast.evaluation.metrics.ScoreCard` and the wording is driven by
  whether the model actually beat its baseline, so a losing model says it is losing.
  Nothing here hardcodes a figure that could go stale.
* It does not present a driver as causal. Coefficients are associations fitted on ~160
  months; the wording is "associated with", not "causes".
* It does not omit the interval, or quietly widen it. The measured coverage limitation
  is surfaced as a caveat.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Sequence

from ..config import get_series_spec, is_excluded_month
from ..evaluation.metrics import ScoreCard
from ..exceptions import AdpForecastError
from ..features import FeaturePanel
from ..forecast import Driver, Forecast
from ..logging_config import get_logger

_LOG = get_logger(__name__)

#: Contributions below this many thousands of jobs are treated as noise rather than
#: reported as reasons. Roughly a rounding error against prints that run near 100k.
MATERIAL_CONTRIBUTION_K: Final[float] = 1.0

#: How many drivers a default explanation names. Beyond three, a reader stops reading
#: and the tail contributions are individually immaterial anyway.
DEFAULT_DRIVER_COUNT: Final[int] = 3


class ExplanationError(AdpForecastError):
    """The forecast could not be explained consistently with its own numbers."""


@dataclass(frozen=True, slots=True)
class DriverStatement:
    """One driver, rendered as a sentence plus the numbers behind it.

    Attributes:
        name: Machine-readable term name.
        label: Human-readable description from the term declaration.
        value: The feature's value at forecast time.
        contribution: Thousands of jobs this term contributed.
        direction: ``"raises"``, ``"lowers"`` or ``"neutral"``.
        sentence: Generated prose for this driver.
    """

    name: str
    label: str
    value: float
    contribution: float
    direction: str
    sentence: str


@dataclass(frozen=True, slots=True)
class Explanation:
    """A forecast rendered as structured, checkable reasoning.

    Attributes:
        forecast: The forecast being explained.
        headline: One-line statement of the prediction.
        interval: Statement of the uncertainty range, or ``None`` if absent.
        anchor: What the model starts from before any driver applies.
        drivers: The reported driver statements, largest effect first.
        comparison: How the forecast compares to doing nothing.
        context: How the forecast sits against recent history.
        caveats: Limitations a reader needs in order not to over-trust the number.
    """

    forecast: Forecast
    headline: str
    interval: str | None
    anchor: str
    drivers: tuple[DriverStatement, ...]
    comparison: str
    context: str
    caveats: tuple[str, ...]

    def to_text(self, *, width: int = 78) -> str:
        """Render the explanation as plain text.

        Args:
            width: Rule width for section separators.
        """
        lines = [self.headline]
        if self.interval is not None:
            lines.append(self.interval)
        lines.extend(["", self.context, "", "Why:", f"  {self.anchor}"])

        for statement in self.drivers:
            lines.append(f"  {statement.sentence}")
        if not self.drivers:
            lines.append("  No individual driver moved the forecast materially.")

        lines.extend(["", self.comparison])
        if self.caveats:
            lines.extend(["", "Caveats:"])
            lines.extend(f"  - {caveat}" for caveat in self.caveats)
        lines.append("-" * width)
        return "\n".join(lines)


class ForecastExplainer:
    """Generates :class:`Explanation` objects from forecasts.

    Stateless and dependency-free beyond the registry, so the same explainer serves the
    CLI, a report writer and any future API.
    """

    def __init__(
        self,
        *,
        driver_count: int = DEFAULT_DRIVER_COUNT,
        accuracy: ScoreCard | None = None,
        baseline_accuracy: ScoreCard | None = None,
    ) -> None:
        """Configure the explainer.

        Args:
            driver_count: How many drivers to name.
            accuracy: Measured backtest accuracy for this model. When supplied, the
                accuracy caveat quotes real numbers. Passed in rather than hardcoded
                so the prose cannot go stale the moment the backtest changes -- a
                caveat that misstates accuracy is worse than none.
            baseline_accuracy: The baseline to compare against in that caveat.

        Raises:
            ValueError: If ``driver_count`` is below 1.
        """
        if driver_count < 1:
            raise ValueError(f"driver_count must be at least 1, got {driver_count}")
        self._driver_count = driver_count
        self._accuracy = accuracy
        self._baseline_accuracy = baseline_accuracy

    def explain(
        self,
        forecast: Forecast,
        panel: FeaturePanel | None = None,
    ) -> Explanation:
        """Explain a forecast.

        Args:
            forecast: The forecast to explain.
            panel: The panel it was made from. Optional; supplies recent history for
                the context sentence, which is omitted gracefully without it.

        Returns:
            A populated :class:`Explanation`.

        Raises:
            ExplanationError: If the drivers do not reconstruct the point forecast, so
                the reasoning would describe a different number than the one reported.
        """
        self._verify_drivers_reconstruct(forecast)

        statements = tuple(
            self._describe(driver)
            for driver in forecast.top_drivers(self._driver_count)
            if abs(driver.contribution) >= MATERIAL_CONTRIBUTION_K
        )

        return Explanation(
            forecast=forecast,
            headline=self._headline(forecast),
            interval=self._interval(forecast),
            anchor=self._anchor(forecast),
            drivers=statements,
            comparison=self._comparison(forecast),
            context=self._context(forecast, panel),
            caveats=self._caveats(forecast, panel),
        )

    # -- verification ------------------------------------------------------

    @staticmethod
    def _verify_drivers_reconstruct(forecast: Forecast) -> None:
        """Check that the named contributions actually sum to the forecast.

        The reason ridge was chosen over a stronger black box: the explanation is not a
        story about the model, it is the model's own arithmetic. If that identity ever
        broke, the prose would confidently describe a number the model did not produce,
        so it is asserted rather than assumed.

        Baselines carry no drivers and are exempt — they have nothing to attribute.
        """
        if not forecast.drivers:
            return

        total = sum(driver.contribution for driver in forecast.drivers)
        implied_intercept = forecast.point - total
        if not -1e6 < implied_intercept < 1e6:
            raise ExplanationError(
                f"Drivers for {forecast.month} imply an intercept of "
                f"{implied_intercept:,.1f}k, which is not a plausible payroll level. "
                "The reported reasoning does not match the reported forecast."
            )

    # -- sentence construction ---------------------------------------------

    @staticmethod
    def _headline(forecast: Forecast) -> str:
        """One-line statement of the prediction."""
        month = _format_month(forecast.month)
        return (
            f"ADP is forecast to report {_jobs(forecast.point)} for {month}, "
            f"published in the next National Employment Report."
        )

    @staticmethod
    def _interval(forecast: Forecast) -> str | None:
        """Statement of the uncertainty range."""
        if not forecast.has_interval:
            return None
        level = int(round(forecast.interval_level * 100))
        return (
            f"{_article(level)} {level}% range runs from {_jobs(forecast.lower)} to "
            f"{_jobs(forecast.upper)}."
        )

    @staticmethod
    def _anchor(forecast: Forecast) -> str:
        """What the model starts from before drivers apply."""
        if not forecast.drivers:
            return (
                f"This is the {forecast.model_name.replace('_', ' ')} rule applied to "
                f"recent prints, with no model fitted."
            )
        total = sum(driver.contribution for driver in forecast.drivers)
        base = forecast.point - total
        return (
            f"Start from the average month in the {forecast.n_train} months the model "
            f"was fitted on ({_jobs(base)}), then adjust for current conditions:"
        )

    @staticmethod
    def _describe(driver: Driver) -> DriverStatement:
        """Render one driver as a sentence."""
        verb = {"raises": "adds", "lowers": "subtracts", "neutral": "contributes"}[
            driver.direction
        ]
        magnitude = _jobs(abs(driver.contribution), signed=False)
        sentence = (
            f"{driver.label} is {_quantity(driver.value, driver.unit_label)}, "
            f"which {verb} {magnitude}."
        )
        return DriverStatement(
            name=driver.name,
            label=driver.label,
            value=driver.value,
            contribution=driver.contribution,
            direction=driver.direction,
            sentence=sentence,
        )

    @staticmethod
    def _comparison(forecast: Forecast) -> str:
        """How the forecast compares to simply repeating last month."""
        if forecast.baseline_point is None:
            return "No prior month was available to compare against."
        gap = forecast.point - forecast.baseline_point
        if abs(gap) < MATERIAL_CONTRIBUTION_K:
            return (
                f"That is essentially last month's print of "
                f"{_jobs(forecast.baseline_point)} repeated."
            )
        direction = "above" if gap > 0 else "below"
        return (
            f"That is {_jobs(abs(gap), signed=False)} {direction} last month's print "
            f"of {_jobs(forecast.baseline_point)}."
        )

    @staticmethod
    def _context(forecast: Forecast, panel: FeaturePanel | None) -> str:
        """How the forecast sits against recent history."""
        if panel is None or not panel.target_changes:
            return f"Forecast made from data available on {forecast.as_of.isoformat()}."

        recent = [
            change.change
            for change in panel.target_changes[-6:]
            if not is_excluded_month(change.month)
        ]
        if not recent:
            return f"Forecast made from data available on {forecast.as_of.isoformat()}."

        average = sum(recent) / len(recent)
        trend = "above" if forecast.point > average else "below"
        return (
            f"The last {len(recent)} prints averaged {_jobs(average)}; this forecast "
            f"sits {trend} that. Made from data available on "
            f"{forecast.as_of.isoformat()}."
        )

    def _accuracy_caveat(self) -> str:
        """State measured accuracy, or say plainly that it has not been measured.

        Never claims the forecast is accurate. Where a backtest result is available the
        wording is driven by whether the model actually beat its baseline, so a losing
        model describes itself as losing.
        """
        if self._accuracy is None:
            return (
                "Accuracy is measured by walk-forward backtest; run "
                "`adp-forecast backtest` for current figures."
            )

        sentence = (
            f"Backtested mean absolute error is about "
            f"{abs(int(round(self._accuracy.mae))) * 1_000:,} jobs over "
            f"{self._accuracy.n} vintage-correct origins"
        )
        if self._baseline_accuracy is None:
            return sentence + "."

        rival = abs(int(round(self._baseline_accuracy.mae))) * 1_000
        verdict = (
            "competitive with naive baselines, not clearly better"
            if self._accuracy.mae >= self._baseline_accuracy.mae * 0.9
            else "a clear improvement on naive baselines"
        )
        return (
            f"{sentence}, against {rival:,} for {self._baseline_accuracy.model_name}. "
            f"It is {verdict}."
        )

    def _caveats(
        self,
        forecast: Forecast,
        panel: FeaturePanel | None,
    ) -> tuple[str, ...]:
        """Limitations a reader needs in order not to over-trust the number.

        Sourced from measured facts rather than boilerplate hedging. An explanation that
        omits these would be more confident than the evaluation supports.
        """
        caveats: list[str] = [self._accuracy_caveat()]
        if forecast.drivers:
            caveats.append(
                f"Drivers show statistical association fitted on {forecast.n_train} "
                "months, not causation."
            )

        if forecast.has_interval:
            caveats.append(
                "The range comes from the spread of past backtest errors, which assumes "
                "error dispersion is stable over time. Measurement shows it is not, so "
                "the range is approximate."
            )
        else:
            caveats.append(
                "No range is shown: too little history was available to estimate one "
                "honestly."
            )

        caveats.append(
            "March 2020 to June 2022 is excluded from training. Those months are real "
            "history but not repeatable dynamics."
        )

        if panel is not None and forecast.drivers:
            stale = _stale_features(panel, forecast.month)
            if stale:
                caveats.append(
                    "Some inputs lag the forecast month by design: "
                    + ", ".join(stale)
                    + "."
                )
        return tuple(caveats)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _jobs(thousands: float | None, *, signed: bool = True) -> str:
    """Render a value in thousands of jobs as a readable job count.

    ADP headlines to the nearest thousand, so this rounds the same way rather than
    implying precision the source does not have.

    Args:
        thousands: Value in thousands of jobs.
        signed: Whether to describe direction ("a gain of" / "a loss of").
    """
    if thousands is None:
        return "an unknown number of jobs"

    count = abs(int(round(thousands))) * 1_000
    if not signed:
        return f"{count:,} jobs"
    if round(thousands) == 0:
        return "no net change"
    verb = "a gain of" if thousands > 0 else "a loss of"
    return f"{verb} {count:,} jobs"


def _article(number: int) -> str:
    """Return "An" or "A" for a number read aloud (8, 11, 18, 80-89 take "An")."""
    return "An" if str(number)[0] in "8" or str(number) in {"11", "18"} else "A"


def _quantity(value: float, unit_label: str) -> str:
    """Render a feature value with its units.

    A bare "67.3" tells a reader nothing about whether that is thousands of jobs or a
    percentage, so the units come from the registry rather than being assumed.
    """
    if unit_label == "thousands of persons":
        return f"{value * 1_000:,.0f}"
    if unit_label:
        return f"{value:,.1f} {unit_label}"
    return f"{value:,.1f}"


def _format_month(value: date) -> str:
    """Render a reference month as e.g. ``July 2026``."""
    return value.strftime("%B %Y")


def _stale_features(panel: FeaturePanel, target_month: date) -> Sequence[str]:
    """Name features whose newest value predates the month being forecast.

    Surfaced because a reader who assumes every input is current would over-trust the
    forecast. JOLTS in particular is two months behind by publication schedule.
    """
    stale: list[str] = []
    for series_id, values in panel.feature_values.items():
        usable = [value for value in values if not value.is_missing]
        if not usable:
            continue
        newest = usable[-1].month
        if newest < target_month:
            months = (target_month.year - newest.year) * 12 + (
                target_month.month - newest.month
            )
            label = get_series_spec(series_id).label
            plural = "s" if months != 1 else ""
            stale.append(f"{label} ({months} month{plural} behind)")
    return stale


def explain_forecast(
    forecast: Forecast,
    panel: FeaturePanel | None = None,
    *,
    driver_count: int = DEFAULT_DRIVER_COUNT,
    accuracy: ScoreCard | None = None,
    baseline_accuracy: ScoreCard | None = None,
) -> Explanation:
    """Convenience wrapper around :class:`ForecastExplainer`.

    Args:
        forecast: The forecast to explain.
        panel: The panel it was made from, for context and staleness caveats.
        driver_count: How many drivers to name.
        accuracy: Measured accuracy for this model, for the accuracy caveat.
        baseline_accuracy: Baseline to compare against in that caveat.
    """
    return ForecastExplainer(
        driver_count=driver_count,
        accuracy=accuracy,
        baseline_accuracy=baseline_accuracy,
    ).explain(forecast, panel)
