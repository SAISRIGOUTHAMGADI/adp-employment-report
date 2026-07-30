"""Shared synthetic panel construction for forecast-layer tests.

Building a realistic :class:`FeaturePanel` by hand is verbose enough that duplicating it
per test file would guarantee drift, so it lives here and every forecast test imports it.
"""

from __future__ import annotations

from datetime import date

from adp_forecast.config import TARGET_SERIES_ID, all_series_ids, get_series_spec
from adp_forecast.domain import MonthlyChange, MonthlyValue, SeriesRole
from adp_forecast.features import FeaturePanel

AS_OF = date(2026, 7, 30)


def shift_months(value: date, offset: int) -> date:
    """Return the first of the month ``offset`` months from ``value``'s month."""
    total = value.year * 12 + (value.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)


def make_panel(
    *,
    months: int = 200,
    start: date = date(2009, 6, 1),
    as_of: date = AS_OF,
    feature_lag_gaps: dict[str, int] | None = None,
    target_values: dict[date, float] | None = None,
) -> FeaturePanel:
    """Build a panel with dense, deterministic history for every registered series.

    Values are smooth deterministic functions rather than random draws so that failures
    are reproducible and a test can predict what the design matrix should contain.

    Args:
        months: How many consecutive months of target history to generate.
        start: First target month.
        as_of: Vantage date stamped on every value.
        feature_lag_gaps: Extra months of staleness per series, on top of its registered
            publication lag. Used to simulate a feature that is unusually far behind.
        target_values: Explicit target changes for specific months, overriding the
            generated series. Used to inject known values such as pandemic outliers.

    Returns:
        A populated :class:`FeaturePanel`.
    """
    gaps = feature_lag_gaps or {}
    overrides = target_values or {}

    target_changes: list[MonthlyChange] = []
    level = 130_000.0
    for index in range(months):
        month = shift_months(start, index)
        change = overrides.get(month, 100.0 + 20.0 * ((index % 7) - 3))
        target_changes.append(
            MonthlyChange(
                series_id=TARGET_SERIES_ID,
                month=month,
                change=change,
                level=level + change,
                previous_level=level,
                as_of=as_of,
            )
        )
        level += change

    last_target = target_changes[-1].month
    feature_values: dict[str, tuple[MonthlyValue, ...]] = {}
    feature_changes: dict[str, tuple[MonthlyChange, ...]] = {}

    for series_id in all_series_ids():
        spec = get_series_spec(series_id)
        if spec.role is SeriesRole.TARGET:
            continue

        # A feature is available through the target month minus its lag, plus any
        # extra staleness the caller asked for.
        lag = spec.publication_lag_months + gaps.get(series_id, 0)
        newest = shift_months(last_target, 1 - lag)

        values: list[MonthlyValue] = []
        changes: list[MonthlyChange] = []
        previous: float | None = None
        index = 0
        month = start
        while month <= newest:
            value = 200.0 + 5.0 * ((index % 11) - 5)
            values.append(
                MonthlyValue(
                    series_id=series_id,
                    month=month,
                    value=value,
                    weeks_used=4 if spec.is_weekly else 1,
                    as_of=as_of,
                )
            )
            if previous is not None:
                changes.append(
                    MonthlyChange(
                        series_id=series_id,
                        month=month,
                        change=value - previous,
                        level=value,
                        previous_level=previous,
                        as_of=as_of,
                    )
                )
            previous = value
            index += 1
            month = shift_months(month, 1)

        feature_values[series_id] = tuple(values)
        feature_changes[series_id] = tuple(changes)

    return FeaturePanel(
        as_of=as_of,
        target_month=shift_months(last_target, 1),
        target_changes=tuple(target_changes),
        feature_values=feature_values,
        feature_changes=feature_changes,
    )
