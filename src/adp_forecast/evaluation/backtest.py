"""Walk-forward backtesting.

Protocol
--------
Expanding-window walk-forward over **real ADP release dates**, pulled from FRED rather
than derived from a "first Wednesday" rule that drifts around holidays. At each origin
every model is refit from scratch on the panel available that day and asked for one
month ahead. No model sees data published after its origin.

Two scorecards, and the difference matters
------------------------------------------
``VINTAGE`` is the headline. Panels come from true point-in-time reads, and each forecast
is scored against the number ADP actually printed that morning. It is limited to ~46
origins because ALFRED holds no as-of record for the target before the 2022 methodology
change — a hard data limit, not a design choice.

``LAG_SHIFTED`` extends coverage to the full history by approximating each origin from
current-vintage data truncated by declared publication lags. It uses *revised* figures
where a real forecaster had first prints, so it is reported as approximate and never as
the headline number.

Comparability
-------------
Models are scored **only on origins where every model produced a forecast**. Models have
different data requirements — the ridge model needs a trailing window the earliest
origins cannot supply — so scoring each on whatever it managed would compare them over
different months and different difficulty. The dropped origins are reported rather than
silently absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Mapping, Sequence

from ..config import ADP_RELEASE_ID, TARGET_SERIES_ID, is_excluded_month
from ..exceptions import AdpForecastError, InsufficientDataError
from ..features import FeaturePanelBuilder
from ..forecast import BASELINE_MODELS, DEFAULT_MODEL, get_model
from ..logging_config import get_logger
from ..storage.port import StoragePort
from ..units import to_thousands
from .metrics import ScoreCard, score

_LOG = get_logger(__name__)


class Scorecard(str, Enum):
    """Which reconstruction the backtest uses for its origins."""

    #: True point-in-time panels, scored against the first print. The headline.
    VINTAGE = "vintage"
    #: Current-vintage data truncated by declared lags. Approximate; wider coverage.
    LAG_SHIFTED = "lag_shifted"


@dataclass(frozen=True, slots=True)
class OriginOutcome:
    """One forecast origin: what each model said, and what actually happened.

    Attributes:
        origin: Forecast origin date. The release date for ``VINTAGE``, the notional
            origin for ``LAG_SHIFTED``.
        target_month: Month forecast.
        actual: Realised month-over-month change, thousands of jobs.
        points: Point forecast per model.
        lowers: Interval lower bound per model, ``None`` where absent.
        uppers: Interval upper bound per model.
        skipped: Models that could not forecast this origin, with the reason.
    """

    origin: date
    target_month: date
    actual: float
    points: Mapping[str, float]
    lowers: Mapping[str, float | None]
    uppers: Mapping[str, float | None]
    skipped: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Results of one backtest run.

    Attributes:
        scorecard: Which reconstruction was used.
        models: Models scored, in report order.
        scores: Per-model results over the common origins.
        outcomes: Every origin attempted, including partially-skipped ones.
        common_origins: Origins where every model produced a forecast — the set the
            scores are computed over.
        interval_level: Nominal coverage the intervals claim.
    """

    scorecard: Scorecard
    models: tuple[str, ...]
    scores: Mapping[str, ScoreCard]
    outcomes: tuple[OriginOutcome, ...]
    common_origins: tuple[date, ...]
    interval_level: float

    @property
    def n_attempted(self) -> int:
        """Origins the backtest tried to score."""
        return len(self.outcomes)

    @property
    def n_scored(self) -> int:
        """Origins actually scored, after requiring all models to succeed."""
        return len(self.common_origins)

    @property
    def n_dropped(self) -> int:
        """Origins dropped because at least one model could not forecast them."""
        return self.n_attempted - self.n_scored

    def best_by_mae(self) -> str:
        """Name of the model with the lowest MAE."""
        return min(self.scores, key=lambda name: self.scores[name].mae)

    def relative_mae(self, model: str, versus: str) -> float:
        """Percentage MAE improvement of ``model`` over ``versus``.

        Positive means ``model`` is more accurate.

        Args:
            model: Model of interest.
            versus: Comparison model.
        """
        reference = self.scores[versus].mae
        return 100.0 * (reference - self.scores[model].mae) / reference


class Backtester:
    """Runs walk-forward backtests over stored observations.

    Depends on :class:`StoragePort`, so it is exercised in tests against an in-memory
    database with no network.
    """

    def __init__(
        self,
        storage: StoragePort,
        builder: FeaturePanelBuilder | None = None,
        *,
        release_id: int = ADP_RELEASE_ID,
    ) -> None:
        """Wire the backtester.

        Args:
            storage: Where observations and release dates are read from.
            builder: Panel builder. Constructed from ``storage`` if omitted.
            release_id: Release whose real dates supply the forecast origins.
        """
        self._storage = storage
        self._builder = builder or FeaturePanelBuilder(storage)
        self._release_id = release_id

    def run(
        self,
        scorecard: Scorecard = Scorecard.VINTAGE,
        *,
        models: Sequence[str] = (DEFAULT_MODEL, *BASELINE_MODELS),
        interval_level: float = 0.80,
        today: date | None = None,
    ) -> BacktestReport:
        """Run a walk-forward backtest.

        Args:
            scorecard: Which reconstruction to use.
            models: Registered model names to score.
            interval_level: Nominal coverage the models' intervals claim, used to
                report the coverage gap.
            today: Upper bound on origins, so scheduled future release dates are
                excluded. Defaults to the current date.

        Returns:
            A populated :class:`BacktestReport`.

        Raises:
            InsufficientDataError: If no origin could be scored at all.
        """
        cutoff = today or date.today()
        origins = self._origins(scorecard, cutoff)
        _LOG.info(
            "Backtesting %d model(s) over %d %s origins",
            len(models),
            len(origins),
            scorecard.value,
        )

        outcomes = [
            outcome
            for outcome in (
                self._evaluate_origin(origin, scorecard, models) for origin in origins
            )
            if outcome is not None
        ]
        if not outcomes:
            raise InsufficientDataError(
                f"No {scorecard.value} origins could be scored. Has ingest run?"
            )

        common = tuple(
            outcome.origin
            for outcome in outcomes
            if all(name in outcome.points for name in models)
        )
        if not common:
            raise InsufficientDataError(
                "No origin was forecast by every model, so no comparable score exists."
            )
        if len(common) < len(outcomes):
            _LOG.warning(
                "Dropped %d of %d origins: not every model could forecast them",
                len(outcomes) - len(common),
                len(outcomes),
            )

        scored = [outcome for outcome in outcomes if outcome.origin in set(common)]
        scores = {
            name: score(
                name,
                [outcome.points[name] for outcome in scored],
                [outcome.actual for outcome in scored],
                [outcome.lowers[name] for outcome in scored],
                [outcome.uppers[name] for outcome in scored],
            )
            for name in models
        }

        return BacktestReport(
            scorecard=scorecard,
            models=tuple(models),
            scores=scores,
            outcomes=tuple(outcomes),
            common_origins=common,
            interval_level=interval_level,
        )

    # -- internals ---------------------------------------------------------

    def _origins(self, scorecard: Scorecard, cutoff: date) -> list[date]:
        """Return candidate forecast origins for a scorecard, ascending."""
        releases = self._storage.read_release_dates(self._release_id, through=cutoff)
        if scorecard is Scorecard.VINTAGE:
            return releases

        # Lag-shifted origins are keyed by target month rather than release date, so
        # the full stored history is reachable.
        observations = self._storage.read_observations(TARGET_SERIES_ID)
        months = sorted({obs.date for obs in observations})
        return [month for month in months if month <= cutoff]

    def _evaluate_origin(
        self,
        origin: date,
        scorecard: Scorecard,
        models: Sequence[str],
    ) -> OriginOutcome | None:
        """Score every model at one origin, or return ``None`` if it is unusable."""
        try:
            panel = (
                self._builder.build_for_release(origin)
                if scorecard is Scorecard.VINTAGE
                else self._builder.build_lag_shifted(origin)
            )
        except AdpForecastError:
            return None

        if is_excluded_month(panel.target_month):
            # Scoring inside the pandemic window would measure a regime no model was
            # trained on, and which we have already argued is not forecastable.
            return None

        actual = self._actual(panel.target_month, panel.latest_target_month, origin, scorecard)
        if actual is None:
            return None

        points: dict[str, float] = {}
        lowers: dict[str, float | None] = {}
        uppers: dict[str, float | None] = {}
        skipped: dict[str, str] = {}

        for name in models:
            try:
                forecast = get_model(name).forecast(panel)
            except AdpForecastError as exc:
                skipped[name] = f"{type(exc).__name__}: {exc}"
                continue
            points[name] = forecast.point
            lowers[name] = forecast.lower
            uppers[name] = forecast.upper

        if not points:
            return None

        return OriginOutcome(
            origin=origin,
            target_month=panel.target_month,
            actual=actual,
            points=points,
            lowers=lowers,
            uppers=uppers,
            skipped=skipped,
        )

    def _actual(
        self,
        target_month: date,
        previous_month: date | None,
        origin: date,
        scorecard: Scorecard,
    ) -> float | None:
        """Return the realised change for ``target_month``, or ``None`` if unavailable.

        For the vintage scorecard the actual is read at the release date itself, so it
        is the number ADP *printed that morning* rather than today's revised figure.
        Scoring against a later revision would measure how well the model anticipated a
        future rebenchmark, which is not forecasting.

        Both levels come from a single snapshot, so the subtraction never crosses
        vintages.
        """
        if previous_month is None:
            return None

        as_of = origin if scorecard is Scorecard.VINTAGE else None
        observations = self._storage.read_observations(TARGET_SERIES_ID, as_of=as_of)
        levels = {
            obs.date: obs.value for obs in observations if obs.value is not None
        }
        current, previous = levels.get(target_month), levels.get(previous_month)
        if current is None or previous is None:
            return None

        scaled_current = to_thousands(TARGET_SERIES_ID, current)
        scaled_previous = to_thousands(TARGET_SERIES_ID, previous)
        if scaled_current is None or scaled_previous is None:
            return None
        return scaled_current - scaled_previous
