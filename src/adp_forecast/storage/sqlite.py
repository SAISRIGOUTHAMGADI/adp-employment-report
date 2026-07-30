"""SQLite implementation of :class:`~adp_forecast.storage.port.StoragePort`.

SQLite is chosen for the key structure, not for the row count. The natural key of an
observation is three-part — ``(series_id, obs_date, realtime_start)`` — and idempotent
re-ingest against that key is one ``INSERT ... ON CONFLICT DO UPDATE``. In a CSV it is
read-everything, deduplicate, rewrite. ``sqlite3`` is in the standard library, so this
adds no dependency to a clone-and-run.

All SQL lives in this module. Downstream code depends on the port, so no query text
escapes the adapter.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Final, Iterable, Sequence

from ..domain import CURRENT_VINTAGE_SENTINEL, Observation
from ..exceptions import StorageIntegrityError, VintageValidationError
from ..logging_config import get_logger
#from .port import IngestCheckpoint

_LOG = get_logger(__name__)

_SCHEMA_PATH: Final[Path] = Path(__file__).with_name("schema.sql")

#: Rows per executemany batch. Bounds peak memory on a large ingest while keeping the
#: number of round trips to the driver small.
_BATCH_SIZE: Final[int] = 5_000

_UPSERT_OBSERVATION: Final[str] = """
INSERT INTO observations
    (series_id, obs_date, realtime_start, realtime_end, value, source, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (series_id, obs_date, realtime_start) DO UPDATE SET
    realtime_end = excluded.realtime_end,
    value        = excluded.value,
    source       = excluded.source,
    fetched_at   = excluded.fetched_at
"""

_UPSERT_RELEASE_DATE: Final[str] = """
INSERT INTO release_dates (release_id, release_date)
VALUES (?, ?)
ON CONFLICT (release_id, release_date) DO NOTHING
"""

# _UPSERT_CHECKPOINT: Final[str] = """
# INSERT INTO ingest_runs (series_id, max_obs_date, row_count, completed_at)
# VALUES (?, ?, ?, ?)
# ON CONFLICT (series_id) DO UPDATE SET
#     max_obs_date = excluded.max_obs_date,
#     row_count    = excluded.row_count,
#     completed_at = excluded.completed_at
# """

_SELECT_OBSERVATION_COLUMNS: Final[str] = (
    "series_id, obs_date, realtime_start, realtime_end, value, source, fetched_at"
)


class SqliteStorage:
    """Stores observations, release dates in SQLite.

    One instance owns one connection. Not thread-safe: ``sqlite3`` connections are
    bound to their creating thread by default, so construct one per thread.

    Usable as a context manager::

        with SqliteStorage(Path("data/adp.db")) as store:
            store.initialise()
            store.upsert_observations(observations)
    """

    def __init__(self, database_path: Path | str) -> None:
        """Open (or create) the database.

        Args:
            database_path: Path to the SQLite file. Parent directories are created
                if needed. ``":memory:"`` is accepted for tests.
        """
        self._path = str(database_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

        # isolation_level=None hands transaction control to this class, so a batch
        # write is one explicit transaction instead of one per statement.
        self._connection = sqlite3.connect(self._path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        _LOG.debug("Opened SQLite store at %s", self._path)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "SqliteStorage":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self._connection.close()

    def initialise(self) -> None:
        """Apply the schema. Idempotent: every statement is ``IF NOT EXISTS``."""
        _LOG.debug("Applying schema from %s", _SCHEMA_PATH.name)
        self._connection.executescript(_SCHEMA_PATH.read_text())

    # -- observations ------------------------------------------------------

    def upsert_observations(self, observations: Sequence[Observation]) -> int:
        """Persist observations idempotently.

        See :meth:`~adp_forecast.storage.port.StoragePort.upsert_observations`.

        Runs in O(n log n) — one B-tree descent per row — with peak memory bounded by
        :data:`_BATCH_SIZE` rather than by the batch length. The whole call is a
        single transaction, so a crash mid-write leaves the store unchanged instead
        of half-updated, which is what makes a resumed ingest safe.
        """
        if not observations:
            _LOG.debug("upsert_observations called with an empty batch; nothing to do")
            return 0

        self._validate_vintage_batch(observations)

        written = 0
        try:
            self._connection.execute("BEGIN")
            for chunk in _chunked(observations, _BATCH_SIZE):
                rows = [
                    (
                        obs.series_id,
                        obs.date.isoformat(),
                        obs.realtime_start.isoformat(),
                        obs.realtime_end.isoformat(),
                        obs.value,
                        obs.source,
                        obs.fetched_at.isoformat(),
                    )
                    for obs in chunk
                ]
                self._connection.executemany(_UPSERT_OBSERVATION, rows)
                written += len(rows)
            self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            self._connection.execute("ROLLBACK")
            raise StorageIntegrityError(
                f"Failed to persist {len(observations)} observations: {exc}"
            ) from exc

        _LOG.info(
            "Persisted %d observations for %s",
            written,
            _describe_series(observations),
        )
        return written

    def read_observations(
        self,
        series_id: str,
        *,
        as_of: date | None = None,
        start: date | None = None,
    ) -> list[Observation]:
        """Read observations for one series.

        See :meth:`~adp_forecast.storage.port.StoragePort.read_observations`.

        Resolves through the primary key: a ``series_id`` seek on a ``WITHOUT ROWID``
        table yields a contiguous run already ordered by
        ``(obs_date, realtime_start)``, so the realtime predicate filters a range that
        must be walked regardless and the ``ORDER BY`` needs no sort. O(log n) to seek
        plus O(k) over that series' stored rows. See ``schema.sql`` for why no
        secondary index on the realtime columns exists.
        """
        clauses = ["series_id = ?"]
        params: list[object] = [series_id]

        if as_of is not None:
            clauses.append("realtime_start <= ? AND realtime_end >= ?")
            as_of_text = as_of.isoformat()
            params.extend((as_of_text, as_of_text))
        if start is not None:
            clauses.append("obs_date >= ?")
            params.append(start.isoformat())

        query = (
            f"SELECT {_SELECT_OBSERVATION_COLUMNS} FROM observations "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY obs_date, realtime_start"
        )
        cursor = self._connection.execute(query, params)
        return [_row_to_observation(row) for row in cursor.fetchall()]

    def count_observations(self, series_id: str | None = None) -> int:
        """Count stored observations, for one series or the whole store."""
        if series_id is None:
            cursor = self._connection.execute("SELECT COUNT(*) FROM observations")
        else:
            cursor = self._connection.execute(
                "SELECT COUNT(*) FROM observations WHERE series_id = ?", (series_id,)
            )
        return int(cursor.fetchone()[0])

    # -- release dates -----------------------------------------------------

    def upsert_release_dates(self, release_id: int, dates: Sequence[date]) -> int:
        """Persist publication dates for a release.

        See :meth:`~adp_forecast.storage.port.StoragePort.upsert_release_dates`.
        """
        if not dates:
            _LOG.warning("No release dates supplied for release_id=%d", release_id)
            return 0

        rows = [(release_id, value.isoformat()) for value in dates]
        try:
            self._connection.execute("BEGIN")
            self._connection.executemany(_UPSERT_RELEASE_DATE, rows)
            self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            self._connection.execute("ROLLBACK")
            raise StorageIntegrityError(
                f"Failed to persist release dates for release_id={release_id}: {exc}"
            ) from exc

        _LOG.info("Persisted %d release dates for release_id=%d", len(rows), release_id)
        return len(rows)

    def read_release_dates(
        self,
        release_id: int,
        *,
        through: date | None = None,
    ) -> list[date]:
        """Read publication dates for a release, ascending.

        See :meth:`~adp_forecast.storage.port.StoragePort.read_release_dates`.
        """
        query = "SELECT release_date FROM release_dates WHERE release_id = ?"
        params: list[object] = [release_id]
        if through is not None:
            query += " AND release_date <= ?"
            params.append(through.isoformat())
        query += " ORDER BY release_date"

        cursor = self._connection.execute(query, params)
        return [date.fromisoformat(row["release_date"]) for row in cursor.fetchall()]

# Scrapping cause we don't need the ingestion runs one as it only held data not queryable
    # # -- checkpoints -------------------------------------------------------

    # def record_checkpoint(self, checkpoint: IngestCheckpoint) -> None:
    #     """Persist the checkpoint for a completed per-series ingest."""
    #     self._connection.execute(
    #         _UPSERT_CHECKPOINT,
    #         (
    #             checkpoint.series_id,
    #             checkpoint.max_obs_date.isoformat() if checkpoint.max_obs_date else None,
    #             checkpoint.row_count,
    #             checkpoint.completed_at.isoformat(),
    #         ),
    #     )
    #     _LOG.debug(
    #         "Checkpointed %s: %d rows through %s",
    #         checkpoint.series_id,
    #         checkpoint.row_count,
    #         checkpoint.max_obs_date,
    #     )

    # def read_checkpoint(self, series_id: str) -> IngestCheckpoint | None:
    #     """Return the stored checkpoint for a series, or ``None``."""
    #     cursor = self._connection.execute(
    #         "SELECT series_id, max_obs_date, row_count, completed_at "
    #         "FROM ingest_runs WHERE series_id = ?",
    #         (series_id,),
    #     )
    #     row = cursor.fetchone()
    #     if row is None:
    #         return None
    #     return IngestCheckpoint(
    #         series_id=row["series_id"],
    #         max_obs_date=(
    #             date.fromisoformat(row["max_obs_date"]) if row["max_obs_date"] else None
    #         ),
    #         row_count=int(row["row_count"]),
    #         completed_at=datetime.fromisoformat(row["completed_at"]),
    #     )

    # -- validation --------------------------------------------------------

    @staticmethod
    def _validate_vintage_batch(observations: Sequence[Observation]) -> None:
        """Reject a batch that carries no real vintage windows.

        A current-vintage fetch reports every row's ``realtime_start`` as the fetch
        date, because FRED answers such a query with a "today..today" window. Genuine
        revision history carries historical publication dates instead. Both share
        ``realtime_end == CURRENT_VINTAGE_SENTINEL``, so the schema cannot tell them
        apart and this check has to.

        The invariant: a genuine batch contains at least one row whose
        ``realtime_start`` predates its own ``fetched_at`` date. Every tracked series
        has years of history across many publication dates, so this cannot fire on
        real vintage data.

        Runs in O(n) with early exit, sharing the pass the caller already makes.

        Raises:
            VintageValidationError: If no row predates its fetch date, or if any row
                carries an inverted window.
        """
        has_historical_vintage = False
        for obs in observations:
            if obs.realtime_end < obs.realtime_start:
                raise VintageValidationError(
                    f"{obs.series_id} @ {obs.date}: realtime_end "
                    f"{obs.realtime_end} precedes realtime_start {obs.realtime_start}"
                )
            if obs.realtime_start < obs.fetched_at.date():
                has_historical_vintage = True

        if not has_historical_vintage:
            series = _describe_series(observations)
            raise VintageValidationError(
                f"Refusing to persist {len(observations)} observations for {series}: "
                "every realtime_start equals the fetch date, which is the signature "
                "of a current-vintage fetch. Those records are display-only and "
                "would silently break point-in-time reconstruction. Re-fetch with "
                "all_vintages=True."
            )


# ---------------------------------------------------------------------------
# Row mapping helpers
# ---------------------------------------------------------------------------


def _row_to_observation(row: sqlite3.Row) -> Observation:
    """Rehydrate an :class:`Observation` from a database row."""
    return Observation(
        series_id=row["series_id"],
        date=date.fromisoformat(row["obs_date"]),
        value=row["value"],
        source=row["source"],
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        realtime_start=date.fromisoformat(row["realtime_start"]),
        realtime_end=date.fromisoformat(row["realtime_end"]),
    )


def _chunked(items: Sequence[Observation], size: int) -> Iterable[Sequence[Observation]]:
    """Yield consecutive slices of ``items`` of at most ``size`` elements.

    Slices rather than copies, so chunking allocates no additional row storage.
    """
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def _describe_series(observations: Sequence[Observation]) -> str:
    """Summarise which series a batch covers, for log messages."""
    unique = {obs.series_id for obs in observations}
    if len(unique) == 1:
        return next(iter(unique))
    return f"{len(unique)} series"


# Re-exported so callers comparing vintage windows do not reach into domain internals.
__all__ = ["SqliteStorage", "CURRENT_VINTAGE_SENTINEL"]
