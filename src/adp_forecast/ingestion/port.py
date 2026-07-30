"""Ingestion contracts.

These protocols are the stable boundary of the ingestion layer. Everything
downstream depends on them; nothing depends on a concrete adapter. Swapping FRED
for a vendor feed, a CSV dump or a database means writing a new adapter that
satisfies :class:`IngestionPort` — no downstream file changes.

Two protocols rather than one, because the capabilities are genuinely separable.
Any source can hand back a time series; only a source with a publication calendar
can say *when* each value was released. Folding both into one interface would
force CSV-backed adapters to stub a method they cannot honour.

Protocols (structural typing) are used instead of abstract base classes so an
adapter needs no import from this module to conform. That keeps the dependency
arrow pointing one way and makes third-party or test doubles trivial.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence, runtime_checkable

from ..domain import Observation


@runtime_checkable
class IngestionPort(Protocol):
    """A source of time-series observations.

    Implementations must be safe to call repeatedly: ingestion is expected to run
    on a schedule and re-fetch overlapping ranges.
    """

    @property
    def source_name(self) -> str:
        """Short identifier stamped onto every :class:`Observation` produced."""
        ...

    def fetch(
        self,
        series_id: str,
        start: date | None = None,
        *,
        all_vintages: bool = False,
    ) -> list[Observation]:
        """Retrieve observations for one series.

        Args:
            series_id: Upstream series identifier.
            start: Earliest reference period to return. ``None`` means the full
                available history.
            all_vintages: When ``False``, return only the currently published
                value for each period — one record per reference date. When
                ``True``, return every historical revision, each carrying the
                realtime window during which it was the published truth. The
                latter is a strict superset: a point-in-time view is recoverable
                from it via :meth:`Observation.known_on`, so callers that need a
                leak-free backtest should ingest with ``all_vintages=True``.

        Returns:
            Observations in ascending order of ``(date, realtime_start)``.

        Raises:
            PermanentIngestionError: Unknown series, rejected credentials, or an
                unparseable payload. Retrying cannot help.
            TransientIngestionError: Timeout, connection failure, throttling or a
                server-side error that survived the adapter's retry policy.
        """
        ...


@runtime_checkable
class ReleaseCalendarPort(Protocol):
    """A source of publication dates for a statistical release.

    Separate from :class:`IngestionPort` because the backtest needs real release
    dates as its forecast origins. Deriving them from a rule ("first Wednesday")
    drifts around holidays, and a forecast origin that is one day late silently
    leaks data that did not exist yet — a corruption that produces no error and
    an implausibly good score.
    """

    def fetch_release_dates(
        self,
        release_id: int,
        start: date | None = None,
    ) -> list[date]:
        """Retrieve the actual publication dates for a release.

        Args:
            release_id: Upstream release identifier.
            start: Earliest release date to return. ``None`` means all.

        Returns:
            Publication dates in ascending order.

        Raises:
            PermanentIngestionError: Unknown release or rejected credentials.
            TransientIngestionError: Transport failure that survived retries.
        """
        ...


def observations_known_on(
    observations: Sequence[Observation],
    as_of: date,
) -> list[Observation]:
    """Filter observations down to the vintage that was published on ``as_of``.

    Shared here rather than in an adapter because it is the reconstruction step
    every consumer of vintage data needs, and it must behave identically no matter
    which source produced the records.

    Runs in O(n) time and allocates only the retained subset. No sorting is
    performed: adapters already return sorted output, so relative order is
    preserved.

    Args:
        observations: Records that may span multiple vintages.
        as_of: The date to reconstruct knowledge at.

    Returns:
        At most one record per ``(series_id, date)`` — the value that was current
        on ``as_of``. Reference periods not yet published by then are absent,
        which is the intended behaviour for a point-in-time feature set.
    """
    return [obs for obs in observations if obs.known_on(as_of)]
