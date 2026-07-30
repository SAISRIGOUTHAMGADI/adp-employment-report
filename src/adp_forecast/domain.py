"""Core domain types shared by every layer.

These types are deliberately free of any dependency on FRED, HTTP, SQL or pandas.
The ingestion, storage, feature and forecast layers all speak in terms of
:class:`Observation`, which is what lets any one of them be replaced without
touching the others.

Vintage model
-------------
An :class:`Observation` is keyed by *three* dimensions, not two:

``(series_id, date, realtime_start)``

``date`` is the reference period the number describes; ``realtime_start`` /
``realtime_end`` are the window during which that value was the published truth.
A statistical agency revising a number does not overwrite history, it closes one
window and opens another. Storing the window lets a backtest ask "what did I know
on 2024-03-06?" and get an honest answer, which is the whole basis of the
evaluation strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Final

#: FRED represents "still the current value" as an open-ended realtime window
#: terminated by this sentinel date. Kept as a named constant because comparing
#: against a bare ``date(9999, 12, 31)`` literal in call sites reads as a bug.
CURRENT_VINTAGE_SENTINEL: Final[date] = date(9999, 12, 31)

#: FRED's own lower bound for realtime windows. Requesting this as
#: ``realtime_start`` is how you ask for the complete revision history.
EARLIEST_REALTIME: Final[date] = date(1776, 7, 4)


class Frequency(str, Enum):
    """Native release frequency of a series.

    Values match FRED's ``frequency_short`` codes so adapters need no translation
    table. Inherits from ``str`` to stay trivially serialisable.
    """

    DAILY = "D"
    WEEKLY = "W"
    MONTHLY = "M"
    QUARTERLY = "Q"
    ANNUAL = "A"


class SeriesRole(str, Enum):
    """Why a series is in the dataset at all.

    Drives downstream behaviour: exactly one ``TARGET`` exists and it is what the
    model predicts; ``FEATURE`` series are model inputs; ``CONTEXT`` series are
    carried for explanation and charts but are not fed to the model.
    """

    TARGET = "target"
    FEATURE = "feature"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """Declarative description of one upstream series.

    Behaviour that varies per series lives here as data rather than as branching
    logic in the adapters. Adding a series is a registry entry, not a code change.

    Attributes:
        series_id: Upstream identifier (e.g. ``ADPMNUSNERSA``).
        role: How the series is used downstream.
        frequency: Native release frequency.
        label: Short human-readable name for CLI output and explanations.
        units: Units the raw values arrive in, as published upstream.
        scale_to_thousands: Multiplier converting raw values to thousands of
            persons. ADP publishes ``Persons`` (132,722,000) while BLS publishes
            ``Thousands of Persons`` (135,613). Normalising at the edge stops a
            1000x error from propagating into the forecast.
        publication_lag_months: How many months stale the series is at forecast
            time. ``JTSJOL`` is 2; most monthly series are 1; weekly series are 0.
        description: Why this series plausibly carries signal. Surfaced in the
            explanation layer so the "why" is sourced from the registry rather
            than hardcoded prose.
    """

    series_id: str
    role: SeriesRole
    frequency: Frequency
    label: str
    units: str
    scale_to_thousands: float = 1.0
    publication_lag_months: int = 1
    description: str = ""

    @property
    def is_weekly(self) -> bool:
        """True when the series needs frequency aggregation before modelling."""
        return self.frequency is Frequency.WEEKLY


@dataclass(frozen=True, slots=True)
class Observation:
    """A single value for one series, one reference period, one vintage window.

    Immutable and slotted: instances are created in bulk during ingestion, so the
    slots layout keeps per-instance memory to the field pointers alone (no
    ``__dict__``), which matters at ~18k rows per full ingest and more later.

    Attributes:
        series_id: Upstream series identifier.
        date: Start of the reference period this value describes.
        value: The observed value, or ``None`` when upstream reports it missing.
        source: Name of the adapter that produced this record (e.g. ``FRED``).
        fetched_at: When *we* retrieved it. Distinct from the vintage window: this
            is provenance for our own cache, not a statement about the data.
        realtime_start: First date on which this value was the published truth.
        realtime_end: Last such date, or :data:`CURRENT_VINTAGE_SENTINEL` if it
            still is.
    """

    series_id: str
    date: date
    value: float | None
    source: str
    fetched_at: datetime
    realtime_start: date
    realtime_end: date = CURRENT_VINTAGE_SENTINEL

    @property
    def is_missing(self) -> bool:
        """True when upstream published no value for this period."""
        return self.value is None

    @property
    def is_current_vintage(self) -> bool:
        """True when this value has not since been revised."""
        return self.realtime_end == CURRENT_VINTAGE_SENTINEL

    def known_on(self, as_of: date) -> bool:
        """Whether this exact value was the published truth on ``as_of``.

        The predicate a leak-free backtest filters on: selecting observations
        where ``known_on(forecast_date)`` reconstructs the dataset as it existed
        that day, revisions and all.

        Args:
            as_of: The date to evaluate knowledge at.
        """
        return self.realtime_start <= as_of <= self.realtime_end

    def shares_vintage_with(self, other: "Observation") -> bool:
        """Whether both values were simultaneously the published truth at some date.

        True exactly when the two realtime windows overlap, which is the precise
        condition for the pair to belong to a single internally-consistent snapshot.

        This is the test that makes cross-vintage arithmetic detectable. A rebenchmark
        restates every historical period at once, so any one snapshot is consistent —
        but subtracting a value from *before* a rebenchmark from one *after* it mixes
        two incompatible bases and produces a number that never existed. Those two
        windows cannot overlap, so this predicate rejects the pair.

        Args:
            other: The observation to compare vintage windows with.
        """
        return (
            self.realtime_start <= other.realtime_end
            and other.realtime_start <= self.realtime_end
        )


@dataclass(frozen=True, slots=True)
class MonthlyValue:
    """One series' value for one calendar month, in canonical units.

    Produced by the feature layer after frequency normalisation. Weekly series are
    aggregated to this shape; monthly series map to it directly.

    Attributes:
        series_id: Series this value belongs to.
        month: First day of the calendar month described.
        value: Value in canonical units (see :mod:`adp_forecast.units`), or ``None``
            when insufficient underlying data existed to form it.
        weeks_used: Underlying observations that contributed. 1 for a monthly series;
            4 or 5 for a complete weekly month; fewer for a partial one.
        as_of: Vantage date this value was reconstructed at. Carried so that a
            downstream consumer cannot silently combine values from different
            snapshots.
    """

    series_id: str
    month: date
    value: float | None
    weeks_used: int
    as_of: date

    @property
    def is_missing(self) -> bool:
        """True when no value could be formed for this month."""
        return self.value is None


@dataclass(frozen=True, slots=True)
class MonthlyChange:
    """A month-over-month change, in canonical units.

    The forecast target. ADP's headline is a change, not a level, so this — not
    :class:`Observation` — is what the model predicts and what the evaluator scores.

    Attributes:
        series_id: Series the change was computed for.
        month: The later of the two months; the period the change describes.
        change: ``level - previous_level``, in canonical units.
        level: Canonical-units level for ``month``.
        previous_level: Canonical-units level for the preceding month.
        as_of: Vantage date both levels were read at. Both came from a single
            snapshot; see :meth:`Observation.shares_vintage_with`.
    """

    series_id: str
    month: date
    change: float
    level: float
    previous_level: float
    as_of: date
