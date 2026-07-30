"""Assembles a leak-free, point-in-time feature panel from stored observations.

One vantage date governs everything. Every series is read with the same ``as_of``
filter, so the panel is exactly the dataset that existed on that date — revisions,
publication lags and all. Nothing is shifted by hand, because nothing needs to be: a
series two months in arrears is simply absent from the snapshot for its missing months.

The one-day rule
----------------
The forecast origin for the print released on date ``R`` is ``R - 1 day``, not ``R``.
Two independent reasons, and either alone is sufficient:

1. The snapshot at ``R`` already contains the reference month released that morning —
   the answer. Reading at ``R`` would hand the model its own target.
2. Other series publish on the same morning, some after ADP's 08:15 ET release, so
   ``R`` can include figures a forecaster genuinely did not have.

:meth:`FeaturePanelBuilder.build_for_release` applies it, so the rule lives in one named
place rather than being re-derived at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Mapping, Sequence

from ..config import TARGET_SERIES_ID, all_series_ids, get_series_spec
from ..domain import (
    CURRENT_VINTAGE_SENTINEL,
    MonthlyChange,
    MonthlyValue,
    SeriesRole,
)
from ..exceptions import InsufficientDataError
from ..logging_config import get_logger
from ..storage.port import StoragePort
from .aggregation import (
    DEFAULT_MIN_WEEKS,
    AggregationMethod,
    aggregate_to_monthly,
    monthly_values_from_monthly_observations,
)
from .changes import change_series, monthly_value_changes

_LOG = get_logger(__name__)

#: Offset from a release date back to the usable forecast origin. See module docstring.
RELEASE_ORIGIN_OFFSET = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class FeaturePanel:
    """Everything knowable on one date, shaped for modelling.

    Attributes:
        as_of: The vantage date. Every value here was the published truth on it.
        target_month: The month being forecast — the one after the newest target
            observation in the snapshot.
        target_changes: Month-over-month changes for the target, ascending. The
            model's own history, and the series a naive baseline extrapolates.
        feature_values: Monthly levels per feature series, in canonical units.
        feature_changes: Month-over-month changes per feature series.
    """

    as_of: date
    target_month: date
    target_changes: tuple[MonthlyChange, ...]
    feature_values: Mapping[str, tuple[MonthlyValue, ...]]
    feature_changes: Mapping[str, tuple[MonthlyChange, ...]]

    @property
    def latest_target_month(self) -> date | None:
        """Newest month with a known target change, or ``None`` if there is none."""
        return self.target_changes[-1].month if self.target_changes else None

    def feature_value_at(self, series_id: str, month: date) -> float | None:
        """Return one feature's level for a month, or ``None`` if absent or missing.

        Args:
            series_id: Feature series to look up.
            month: Calendar month, as any date within it.
        """
        target = month.replace(day=1)
        for value in self.feature_values.get(series_id, ()):
            if value.month == target:
                return value.value
        return None

    def months_available(self, series_id: str) -> int:
        """Count months with a usable value for a series.

        Args:
            series_id: Series to count. Accepts the target or any feature.
        """
        if series_id == TARGET_SERIES_ID:
            return len(self.target_changes)
        return sum(
            1 for value in self.feature_values.get(series_id, ()) if not value.is_missing
        )


class FeaturePanelBuilder:
    """Builds :class:`FeaturePanel` instances from a :class:`StoragePort`.

    Depends on the storage protocol rather than on SQLite, so the same builder serves a
    backtest reading from disk and a unit test reading from an in-memory fake.

    Frequency handling is driven by the series registry: a series declared weekly goes
    through the aggregation rule, a monthly one maps straight across. No series ID is
    named here, so tracking a new indicator needs no change to this class.
    """

    def __init__(
        self,
        storage: StoragePort,
        *,
        method: AggregationMethod = AggregationMethod.CALENDAR_MONTH_MEAN,
        min_weeks: int = DEFAULT_MIN_WEEKS,
    ) -> None:
        """Wire the builder.

        Args:
            storage: Where observations are read from.
            method: Weekly-to-monthly aggregation rule.
            min_weeks: Minimum contributing weeks for a weekly-derived month.
        """
        self._storage = storage
        self._method = method
        self._min_weeks = min_weeks

    def build_for_release(
        self,
        release_date: date,
        *,
        series_ids: Sequence[str] | None = None,
    ) -> FeaturePanel:
        """Build the panel a forecaster could have had for a given release.

        Applies the one-day rule: the origin is ``release_date - 1 day``. See the
        module docstring for why.

        Args:
            release_date: The date the print was published.
            series_ids: Series to include. Defaults to the whole registry.

        Returns:
            The panel as of the day before the release.
        """
        return self.build(release_date - RELEASE_ORIGIN_OFFSET, series_ids=series_ids)

    def build_lag_shifted(
        self,
        target_month: date,
        *,
        current_as_of: date | None = None,
        series_ids: Sequence[str] | None = None,
    ) -> FeaturePanel:
        """Approximate a historical panel using current-vintage data and declared lags.

        The fallback for origins where true vintage data does not exist. ALFRED holds
        no as-of record for ``ADPMNUSNERSA`` before the 2022 methodology change, so
        :meth:`build` can only reach ~46 origins. This reconstructs the rest by taking
        today's values and truncating each series to the months its registered
        ``publication_lag_months`` says would have been published.

        **This is approximate and must be reported as such.** It uses *revised* values
        where a real forecaster had first prints, so it cannot measure the effect of
        revisions and will tend to flatter any model that benefits from cleaner inputs.
        It exists to give a longer, clearly-caveated secondary scorecard — never to
        replace the vintage-correct one.

        Args:
            target_month: The month to forecast.
            current_as_of: Vantage for reading current-vintage data. Defaults to the
                open-ended sentinel, i.e. the latest published value.
            series_ids: Series to include. Defaults to the whole registry.

        Returns:
            A panel whose ``as_of`` is the day before ``target_month``'s notional
            release, truncated per declared lag.

        Raises:
            InsufficientDataError: If fewer than two target observations precede
                ``target_month``.
        """
        vantage = current_as_of or CURRENT_VINTAGE_SENTINEL
        requested = tuple(series_ids) if series_ids is not None else all_series_ids()
        first_of_target = target_month.replace(day=1)

        target_observations = [
            obs
            for obs in self._storage.read_observations(TARGET_SERIES_ID, as_of=vantage)
            if obs.date < first_of_target
        ]
        if len(target_observations) < 2:
            raise InsufficientDataError(
                f"{TARGET_SERIES_ID} has {len(target_observations)} observation(s) "
                f"before {first_of_target}; at least 2 are needed."
            )

        notional_as_of = first_of_target - RELEASE_ORIGIN_OFFSET
        target_changes = change_series(target_observations, as_of=vantage)
        if not target_changes:
            raise InsufficientDataError(
                f"No target changes available before {first_of_target}."
            )

        feature_values: dict[str, tuple[MonthlyValue, ...]] = {}
        feature_changes: dict[str, tuple[MonthlyChange, ...]] = {}

        for series_id in requested:
            spec = get_series_spec(series_id)
            if spec.role is SeriesRole.TARGET:
                continue

            # The newest month a forecaster would have had for this series.
            newest = _shift_months(first_of_target, -spec.publication_lag_months)
            # Truncate at the *end* of that month, not its first day: weekly series are
            # dated by week-ending Saturday, so cutting at the 1st would discard every
            # week of the month we mean to keep.
            cutoff = _last_day_of_month(newest)
            observations = [
                obs
                for obs in self._storage.read_observations(series_id, as_of=vantage)
                if obs.date <= cutoff
            ]
            values = (
                []
                if not observations
                else self._normalise(series_id, observations, notional_as_of)
            )
            trimmed = [value for value in values if value.month <= newest]
            feature_values[series_id] = tuple(trimmed)
            feature_changes[series_id] = tuple(
                monthly_value_changes(trimmed, as_of=notional_as_of)
            )

        return FeaturePanel(
            as_of=notional_as_of,
            target_month=first_of_target,
            target_changes=tuple(
                MonthlyChange(
                    series_id=change.series_id,
                    month=change.month,
                    change=change.change,
                    level=change.level,
                    previous_level=change.previous_level,
                    as_of=notional_as_of,
                )
                for change in target_changes
            ),
            feature_values=MappingProxyType(feature_values),
            feature_changes=MappingProxyType(feature_changes),
        )

    def build(
        self,
        as_of: date,
        *,
        series_ids: Sequence[str] | None = None,
    ) -> FeaturePanel:
        """Build the panel of everything knowable on ``as_of``.

        Args:
            as_of: The vantage date. Passed unchanged to storage as the point-in-time
                filter; no lag shifting is applied because the snapshot already
                reflects what had actually been published.
            series_ids: Series to include. Defaults to the whole registry.

        Returns:
            A populated :class:`FeaturePanel`.

        Raises:
            InsufficientDataError: If the target has fewer than two observations on
                ``as_of``, so no change can be formed and there is nothing to forecast
                from.
        """
        requested = tuple(series_ids) if series_ids is not None else all_series_ids()

        target_changes = self._build_target_changes(as_of)
        target_month = _next_month(target_changes[-1].month)

        feature_values: dict[str, tuple[MonthlyValue, ...]] = {}
        feature_changes: dict[str, tuple[MonthlyChange, ...]] = {}

        for series_id in requested:
            spec = get_series_spec(series_id)
            if spec.role is SeriesRole.TARGET:
                continue
            values = self._monthly_values(series_id, as_of)
            feature_values[series_id] = tuple(values)
            feature_changes[series_id] = tuple(
                monthly_value_changes(values, as_of=as_of)
            )

        panel = FeaturePanel(
            as_of=as_of,
            target_month=target_month,
            target_changes=tuple(target_changes),
            feature_values=MappingProxyType(feature_values),
            feature_changes=MappingProxyType(feature_changes),
        )
        _LOG.info(
            "Panel as_of=%s: forecasting %s from %d target changes and %d features",
            as_of.isoformat(),
            target_month.isoformat(),
            len(target_changes),
            len(feature_values),
        )
        return panel

    # -- internals ---------------------------------------------------------

    def _build_target_changes(self, as_of: date) -> list[MonthlyChange]:
        """Read the target snapshot and difference it within that single vintage."""
        observations = self._storage.read_observations(TARGET_SERIES_ID, as_of=as_of)
        if len(observations) < 2:
            raise InsufficientDataError(
                f"{TARGET_SERIES_ID} had {len(observations)} observation(s) as of "
                f"{as_of}; at least 2 are needed to form a month-over-month change. "
                "Has the ingest run for this date range?"
            )

        changes = change_series(observations, as_of=as_of)
        if not changes:
            raise InsufficientDataError(
                f"{TARGET_SERIES_ID} yielded no month-over-month changes as of "
                f"{as_of}, despite {len(observations)} observations."
            )
        return changes

    def _monthly_values(self, series_id: str, as_of: date) -> list[MonthlyValue]:
        """Read one feature and normalise it to monthly, per its declared frequency."""
        observations = self._storage.read_observations(series_id, as_of=as_of)
        if not observations:
            _LOG.warning("%s has no observations as of %s", series_id, as_of)
            return []
        return self._normalise(series_id, observations, as_of)

    def _normalise(
        self,
        series_id: str,
        observations: list,
        as_of: date,
    ) -> list[MonthlyValue]:
        """Apply the frequency rule the registry declares for a series.

        Shared by both build paths so a weekly series is aggregated identically whether
        the panel came from a true vintage read or the lag-shifted approximation.
        """
        spec = get_series_spec(series_id)
        if spec.is_weekly:
            return aggregate_to_monthly(
                observations,
                method=self._method,
                min_weeks=self._min_weeks,
                as_of=as_of,
            )
        return monthly_values_from_monthly_observations(observations, as_of=as_of)


def _next_month(value: date) -> date:
    """Return the first day of the month following ``value``'s month."""
    first = value.replace(day=1)
    if first.month == 12:
        return first.replace(year=first.year + 1, month=1)
    return first.replace(month=first.month + 1)


def _shift_months(value: date, offset: int) -> date:
    """Return the first of the month ``offset`` months from ``value``'s month."""
    total = value.year * 12 + (value.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)


def _last_day_of_month(value: date) -> date:
    """Return the last day of ``value``'s calendar month."""
    return _shift_months(value, 1) - timedelta(days=1)
