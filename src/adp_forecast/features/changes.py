"""Month-over-month change computation, structurally prevented from mixing vintages.

The forecast target is a change, not a level: ADP headlines "private employers added
98,000 jobs", and FRED stores the level it is derived from. Computing that change is a
subtraction, and a subtraction is exactly where the project's highest-impact bug lives.

The bug
-------
Each January, ADP rebenchmarks to QCEW and **restates the entire history** of the
series. Any one snapshot is therefore internally consistent, and a change computed
inside it is correct — including across a January. But subtracting a pre-rebenchmark
level from a post-rebenchmark one blends two incompatible bases. Measured on real data,
that fabricates:

======================  ==================  ============
Reference month         Cross-vintage diff  True change
======================  ==================  ============
2023-01                 +4,616k             +106k
2024-01                 +1,926k             +107k
2026-01                 -2,307k             +22k
======================  ==================  ============

The guard
---------
Rather than masking January — which discards real observations to avoid a mistake
nobody should be making — this module refuses to subtract across vintages at all.
Every function requires an explicit ``as_of``, and both operands must have been the
published truth on that date. A cross-vintage pair raises
:class:`~adp_forecast.exceptions.VintageMismatchError`.

That makes the vantage point a mandatory argument rather than an assumption, on the same
principle as the units choke point in :mod:`adp_forecast.units`: make the error
impossible to reintroduce quietly instead of relying on a comment or a default-off flag.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from ..domain import MonthlyChange, MonthlyValue, Observation
from ..exceptions import VintageMismatchError
from ..logging_config import get_logger
from ..units import to_thousands

_LOG = get_logger(__name__)


def month_over_month_change(
    current: Observation,
    previous: Observation,
    *,
    as_of: date,
) -> MonthlyChange | None:
    """Compute one month-over-month change from two observations of one snapshot.

    Args:
        current: Observation for the later month.
        previous: Observation for the immediately preceding month.
        as_of: The vantage date. Both operands must have been the published truth on
            this date; it is required rather than inferred so the caller has to state
            which snapshot the arithmetic belongs to.

    Returns:
        The change in canonical units, or ``None`` if either level is missing upstream.

    Raises:
        VintageMismatchError: If the operands are from different series, are not
            consecutive months, or were not both current on ``as_of``.
    """
    if current.series_id != previous.series_id:
        raise VintageMismatchError(
            f"Cannot difference across series: {previous.series_id} -> "
            f"{current.series_id}"
        )
    if _month_start(previous.date) != _previous_month(current.date):
        raise VintageMismatchError(
            f"{current.series_id}: {previous.date} does not immediately precede "
            f"{current.date}; a month-over-month change requires consecutive months"
        )

    _require_shared_vintage(current, previous, as_of)

    current_level = to_thousands(current.series_id, current.value)
    previous_level = to_thousands(previous.series_id, previous.value)
    if current_level is None or previous_level is None:
        return None

    return MonthlyChange(
        series_id=current.series_id,
        month=_month_start(current.date),
        change=current_level - previous_level,
        level=current_level,
        previous_level=previous_level,
        as_of=as_of,
    )


def change_series(
    observations: Sequence[Observation],
    *,
    as_of: date,
) -> list[MonthlyChange]:
    """Compute the full month-over-month change series from one snapshot.

    Args:
        observations: Monthly observations for a single series, already filtered to a
            single vintage (i.e. read with an ``as_of`` filter).
        as_of: The vantage date the snapshot represents.

    Returns:
        Changes ordered by month, one per consecutive pair. Pairs where either level is
        missing, or which straddle a gap in the reference periods, are omitted — a
        change across a gap is not a month-over-month change.

    Raises:
        VintageMismatchError: If the input mixes series or vintages, or if any pair
            was not jointly current on ``as_of``.
    """
    if len(observations) < 2:
        return []

    ordered = sorted(observations, key=lambda obs: obs.date)
    changes: list[MonthlyChange] = []
    skipped_gaps = 0

    for previous, current in zip(ordered, ordered[1:]):
        if _month_start(previous.date) != _previous_month(current.date):
            # A non-consecutive pair is a hole in the series, not a monthly change.
            skipped_gaps += 1
            continue
        change = month_over_month_change(current, previous, as_of=as_of)
        if change is not None:
            changes.append(change)

    if skipped_gaps:
        _LOG.warning(
            "%s: skipped %d non-consecutive month pair(s) when computing changes",
            ordered[0].series_id,
            skipped_gaps,
        )
    return changes


def monthly_value_changes(
    values: Sequence[MonthlyValue],
    *,
    as_of: date | None = None,
) -> list[MonthlyChange]:
    """Compute month-over-month changes from already-aggregated monthly values.

    Used for the weekly-derived features, whose monthly form comes out of
    :mod:`adp_forecast.features.aggregation` rather than straight from storage. Values
    are already in canonical units, so no conversion happens here.

    Args:
        values: Monthly values for one series, all from one snapshot.
        as_of: Vantage date for the results. Defaults to the shared ``as_of`` of the
            inputs.

    Returns:
        Changes ordered by month, skipping pairs with a missing side or a month gap.

    Raises:
        VintageMismatchError: If the inputs mix series, or carry differing ``as_of``
            dates — which would mean they came from different snapshots.
    """
    if len(values) < 2:
        return []

    series_ids = {value.series_id for value in values}
    if len(series_ids) > 1:
        raise VintageMismatchError(
            f"Cannot difference across series: {sorted(series_ids)}"
        )

    vantages = {value.as_of for value in values}
    if len(vantages) > 1:
        raise VintageMismatchError(
            f"Monthly values span multiple snapshots ({sorted(vantages)}); "
            "differencing them would mix vintages"
        )
    effective_as_of = as_of or next(iter(vantages))

    ordered = sorted(values, key=lambda value: value.month)
    changes: list[MonthlyChange] = []

    for previous, current in zip(ordered, ordered[1:]):
        if previous.month != _previous_month(current.month):
            continue
        if previous.value is None or current.value is None:
            continue
        changes.append(
            MonthlyChange(
                series_id=current.series_id,
                month=current.month,
                change=current.value - previous.value,
                level=current.value,
                previous_level=previous.value,
                as_of=effective_as_of,
            )
        )
    return changes


# ---------------------------------------------------------------------------
# Guards and helpers
# ---------------------------------------------------------------------------


def _require_shared_vintage(
    current: Observation,
    previous: Observation,
    as_of: date,
) -> None:
    """Verify both operands were the published truth on ``as_of``.

    Two checks rather than one, because they fail for different reasons and the
    messages need to say which:

    * ``known_on`` catches an operand that was not yet published, or had already been
      superseded, on the stated vantage date.
    * ``shares_vintage_with`` catches operands whose windows do not overlap at all —
      the rebenchmark case — even if ``as_of`` were somehow satisfied.

    O(1).

    Raises:
        VintageMismatchError: If either check fails.
    """
    if not current.shares_vintage_with(previous):
        raise VintageMismatchError(
            f"{current.series_id}: refusing to subtract across vintages. "
            f"{previous.date} was published {previous.realtime_start}..{previous.realtime_end} "
            f"and {current.date} was published {current.realtime_start}..{current.realtime_end}; "
            "these windows never overlap, so the two levels sit on different "
            "benchmark bases and their difference was never published."
        )

    for label, obs in (("current", current), ("previous", previous)):
        if not obs.known_on(as_of):
            raise VintageMismatchError(
                f"{obs.series_id}: {label} operand for {obs.date} was not the "
                f"published value on as_of={as_of} (its vintage window is "
                f"{obs.realtime_start}..{obs.realtime_end}). Read the snapshot with "
                "as_of before differencing."
            )


def _month_start(value: date) -> date:
    """Return the first day of ``value``'s calendar month."""
    return value.replace(day=1)


def _previous_month(value: date) -> date:
    """Return the first day of the month preceding ``value``'s month."""
    first = value.replace(day=1)
    if first.month == 1:
        return first.replace(year=first.year - 1, month=12)
    return first.replace(month=first.month - 1)
