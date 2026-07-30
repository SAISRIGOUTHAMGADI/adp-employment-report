"""Frequency normalisation: weekly observations to monthly values.

``ICSA`` and ``CCSA`` are published weekly (dated by week-ending Saturday) while the
forecast target is monthly, so the two must be reconciled before modelling.

The method is a swappable rule behind :func:`aggregate_to_monthly`. The default is
:attr:`AggregationMethod.CALENDAR_MONTH_MEAN`; adding an alternative is one function
plus one enum member, with no change at any call site.

Why the calendar-month mean is the default
------------------------------------------
Claims are jumpy week to week, and any single week is vulnerable to a holiday or a
one-off spike. Averaging every week in the month uses all the information and yields a
steadier signal — the same reasoning behind the four-week moving average that is the
standard way claims are read in practice.

The alternative — the BLS reference week containing the 12th — matches how payrolls are
measured on paper, but keeps one week and discards the rest. We are predicting the
*move*, not reconstructing the official number, so the steadier feature wins. Both are
fully published before the ADP release date, so leakage favours neither.

A **mean** rather than a sum matters more than it looks: measured across the stored
history, 138 months contain 4 week-ending Saturdays and 73 contain 5. A sum would inject
a spurious 25% swing driven purely by calendar drift.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from enum import Enum
from typing import Callable, Final, Mapping, Sequence

from ..domain import MonthlyValue, Observation
from ..exceptions import VintageMismatchError
from ..logging_config import get_logger
from ..units import to_thousands

_LOG = get_logger(__name__)

#: Minimum contributing weeks before a month yields a value. The month currently in
#: progress is usually partial at forecast time, and one week of claims is noise rather
#: than signal, so a month below this threshold is reported missing instead of guessed.
DEFAULT_MIN_WEEKS: Final[int] = 2


class AggregationMethod(str, Enum):
    """How weekly observations collapse to a monthly value.

    Inherits from ``str`` so a method survives serialisation into a run manifest or a
    CLI argument unchanged.
    """

    CALENDAR_MONTH_MEAN = "calendar_month_mean"


def aggregate_to_monthly(
    observations: Sequence[Observation],
    *,
    method: AggregationMethod = AggregationMethod.CALENDAR_MONTH_MEAN,
    min_weeks: int = DEFAULT_MIN_WEEKS,
    as_of: date | None = None,
) -> list[MonthlyValue]:
    """Collapse weekly observations into monthly values in canonical units.

    Args:
        observations: Weekly observations for a single series, drawn from **one**
            snapshot. Must contain at most one record per reference date.
        method: Which aggregation rule to apply.
        min_weeks: Minimum contributing weeks for a month to yield a value.
        as_of: Vantage date stamped onto the results. Defaults to the latest
            ``realtime_start`` present, which is the snapshot's effective date.

    Returns:
        Monthly values ordered by month. Months below ``min_weeks`` are present with
        ``value=None`` rather than omitted, so a gap is visible instead of silent.

    Raises:
        VintageMismatchError: If the input mixes series, or contains more than one
            vintage of the same reference date. Averaging across vintages would blend
            two incompatible bases into one number.
        ValueError: If ``min_weeks`` is below 1, or ``method`` has no registered rule.
    """
    if min_weeks < 1:
        raise ValueError(f"min_weeks must be at least 1, got {min_weeks}")

    if not observations:
        return []

    series_id = _require_single_series(observations)
    _reject_multiple_vintages(observations)

    rule = _RULES.get(method)
    if rule is None:
        raise ValueError(
            f"No aggregation rule registered for {method!r}. "
            f"Available: {', '.join(sorted(rule.value for rule in _RULES))}"
        )

    effective_as_of = as_of or max(obs.realtime_start for obs in observations)
    results = rule(observations, series_id, min_weeks, effective_as_of)

    incomplete = [value.month for value in results if value.is_missing]
    if incomplete:
        _LOG.debug(
            "%s: %d month(s) below the %d-week minimum, reported missing: %s",
            series_id,
            len(incomplete),
            min_weeks,
            ", ".join(month.isoformat() for month in incomplete),
        )
    return results


def _calendar_month_mean(
    observations: Sequence[Observation],
    series_id: str,
    min_weeks: int,
    as_of: date,
) -> list[MonthlyValue]:
    """Average every observation whose week-ending date falls in the month.

    Weeks are assigned by their week-ending date, so the week ending 2026-07-04 counts
    entirely as July. No week is split across months or counted twice, which keeps the
    assignment total and unambiguous at the cost of a few days of June activity landing
    in July — immaterial against a monthly mean, and consistent across all months.

    Missing observations (upstream ``"."``) are excluded from both the sum and the
    count, so a month with one missing week averages its remaining weeks rather than
    being poisoned.

    Runs in O(n) with one pass to bucket and one pass over the buckets; memory is O(m)
    in the number of distinct months, not O(n).
    """
    sums: dict[date, float] = defaultdict(float)
    counts: dict[date, int] = defaultdict(int)

    for obs in observations:
        month = _month_start(obs.date)
        counts.setdefault(month, 0)  # register the month even if every week is missing
        value = to_thousands(series_id, obs.value)
        if value is None:
            continue
        sums[month] += value
        counts[month] += 1

    return [
        MonthlyValue(
            series_id=series_id,
            month=month,
            value=(sums[month] / counts[month] if counts[month] >= min_weeks else None),
            weeks_used=counts[month],
            as_of=as_of,
        )
        for month in sorted(counts)
    ]


#: Registry of aggregation rules. The seam: a new method is an entry here plus an enum
#: member, and no call site changes.
_RULES: Final[
    Mapping[
        AggregationMethod,
        Callable[[Sequence[Observation], str, int, date], list[MonthlyValue]],
    ]
] = {
    AggregationMethod.CALENDAR_MONTH_MEAN: _calendar_month_mean,
}


def monthly_values_from_monthly_observations(
    observations: Sequence[Observation],
    *,
    as_of: date | None = None,
) -> list[MonthlyValue]:
    """Map already-monthly observations onto :class:`MonthlyValue`.

    The counterpart to :func:`aggregate_to_monthly` for series that need no frequency
    change. Exists so downstream code handles one shape regardless of native frequency
    rather than branching on it.

    Args:
        observations: Monthly observations for a single series, from one snapshot.
        as_of: Vantage date stamped onto the results. Defaults to the latest
            ``realtime_start`` present.

    Returns:
        Monthly values ordered by month, with ``weeks_used=1`` for present values and
        ``0`` for missing ones.

    Raises:
        VintageMismatchError: If the input mixes series or vintages.
    """
    if not observations:
        return []

    series_id = _require_single_series(observations)
    _reject_multiple_vintages(observations)
    effective_as_of = as_of or max(obs.realtime_start for obs in observations)

    return [
        MonthlyValue(
            series_id=series_id,
            month=_month_start(obs.date),
            value=to_thousands(series_id, obs.value),
            weeks_used=0 if obs.value is None else 1,
            as_of=effective_as_of,
        )
        for obs in sorted(observations, key=lambda obs: obs.date)
    ]


# ---------------------------------------------------------------------------
# Guards and helpers
# ---------------------------------------------------------------------------


def _require_single_series(observations: Sequence[Observation]) -> str:
    """Return the one series ID present, rejecting a mixed batch.

    Raises:
        VintageMismatchError: If more than one series is present. Aggregating across
            series is always a caller bug, and the resulting number would be
            meaningless rather than merely wrong.
    """
    unique = {obs.series_id for obs in observations}
    if len(unique) > 1:
        raise VintageMismatchError(
            f"Cannot aggregate a batch spanning multiple series: {sorted(unique)}"
        )
    return next(iter(unique))


def _reject_multiple_vintages(observations: Sequence[Observation]) -> None:
    """Reject input holding more than one vintage of the same reference date.

    A snapshot has exactly one value per reference period. More than one means the
    caller passed unfiltered vintage history, and averaging it would blend a
    pre-rebenchmark basis with a post-rebenchmark one.

    O(n) time and O(n) worst-case memory in the number of distinct reference dates.

    Raises:
        VintageMismatchError: If any reference date appears more than once.
    """
    seen: set[date] = set()
    for obs in observations:
        if obs.date in seen:
            raise VintageMismatchError(
                f"{obs.series_id} @ {obs.date}: reference date appears more than once, "
                "so this batch spans multiple vintages. Read with an as_of filter "
                "before aggregating."
            )
        seen.add(obs.date)


def _month_start(value: date) -> date:
    """Return the first day of ``value``'s calendar month."""
    return value.replace(day=1)
