"""Storage contracts.

The stable boundary of the persistence layer. ``IngestService`` and every downstream
consumer depend on :class:`StoragePort`, never on SQLite, so swapping in Postgres or
DuckDB is a new adapter and no downstream change.

Structural protocols rather than abstract base classes, matching
:mod:`adp_forecast.ingestion.port`: an adapter needs no import from this module to
conform, which keeps the dependency arrow pointing one way.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence, runtime_checkable

from ..domain import Observation


# @dataclass(frozen=True, slots=True)
# class IngestCheckpoint:
#     """Record of the last completed ingest for one series.

#     Attributes:
#         series_id: The series this checkpoint describes.
#         max_obs_date: Newest reference period stored, or ``None`` if the series held
#             no observations.
#         row_count: Rows written by that run.
#         completed_at: When the run finished.
#     """

#     series_id: str
#     max_obs_date: date | None
#     row_count: int
#     completed_at: datetime


@runtime_checkable
class StoragePort(Protocol):
    """Persistence for observations, release dates.

    Implementations must make writes idempotent: ingestion re-fetches overlapping
    ranges on every run, so writing the same batch twice must leave the store in the
    same state as writing it once.
    """

    def initialise(self) -> None:
        """Create the schema if absent. Safe to call on an existing store."""
        ...

    def upsert_observations(self, observations: Sequence[Observation]) -> int:
        """Persist observations, updating any whose vintage window has changed.

        Args:
            observations: Records to write. Must carry genuine vintage windows.

        Returns:
            Number of rows written.

        Raises:
            VintageValidationError: If the batch looks like a current-vintage fetch
                rather than full revision history. Those records report the fetch
                date as ``realtime_start``, so persisting them would silently break
                point-in-time reconstruction while looking identical to real data.
            StorageIntegrityError: If the write violates a schema invariant.
        """
        ...

    def read_observations(
        self,
        series_id: str,
        *,
        as_of: date | None = None,
        start: date | None = None,
    ) -> list[Observation]:
        """Read observations for one series.

        Args:
            series_id: Series to read.
            as_of: When given, return only the vintage published on that date — one
                row per reference period, as the data stood that day. This is the
                leak-free read a backtest must use. When ``None``, return every
                stored vintage.
            start: Earliest reference period to include.

        Returns:
            Observations ordered by ``(obs_date, realtime_start)``.
        """
        ...

    def upsert_release_dates(self, release_id: int, dates: Sequence[date]) -> int:
        """Persist publication dates for a release.

        Args:
            release_id: Upstream release identifier.
            dates: Publication dates. May include scheduled future dates; filtering
                is the caller's responsibility.

        Returns:
            Number of rows written.
        """
        ...

    def read_release_dates(
        self,
        release_id: int,
        *,
        through: date | None = None,
    ) -> list[date]:
        """Read publication dates for a release, ascending.

        Args:
            release_id: Upstream release identifier.
            through: Latest date to include. Pass today's date to exclude the
                scheduled future dates FRED returns.
        """
        ...

# these are the read/write interface for that ingestion unread data, so they got scrapped.
    # def record_checkpoint(self, checkpoint: IngestCheckpoint) -> None:
    #     """Persist the checkpoint for a completed per-series ingest."""
    #     ...

    # def read_checkpoint(self, series_id: str) -> IngestCheckpoint | None:
    #     """Return the stored checkpoint for a series, or ``None`` if never ingested."""
    #     ...

    def count_observations(self, series_id: str | None = None) -> int:
        """Count stored observations, for one series or the whole store.

        Args:
            series_id: Series to count, or ``None`` for all series.
        """
        ...
