"""Ingest orchestration.

The one place ingestion and storage meet. Both are injected as protocols, so this
service knows nothing about FRED or SQLite and is testable with in-memory fakes —
which is also what lets the whole pipeline be exercised without a network or a file.

Everything here loops over the series registry rather than naming series inline, so
tracking a new indicator is a registry entry and no change to this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Sequence

from .config import ADP_RELEASE_ID, all_series_ids
from .domain import Observation
from .exceptions import AdpForecastError
from .ingestion.port import IngestionPort, ReleaseCalendarPort
from .logging_config import get_logger
from .storage.port import IngestCheckpoint, StoragePort

_LOG = get_logger(__name__)

#: Earliest reference period worth ingesting. 2009 gives a full year of run-up before
#: the ADP series begins in 2010-01, so weekly indicators have history to aggregate
#: from at the target's first observation.
DEFAULT_START = date(2009, 1, 1)


@dataclass(frozen=True, slots=True)
class SeriesIngestResult:
    """Outcome of ingesting one series.

    Attributes:
        series_id: The series ingested.
        rows_written: Observations persisted.
        max_obs_date: Newest reference period seen, or ``None`` if none returned.
        error: The failure that stopped this series, or ``None`` on success. Held
            rather than raised so one bad series does not abort the whole run.
    """

    series_id: str
    rows_written: int
    max_obs_date: date | None
    error: Exception | None = None

    @property
    def succeeded(self) -> bool:
        """True when the series ingested without error."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Aggregate outcome of an ingest run.

    Attributes:
        results: Per-series outcomes, in registry order.
        release_dates_written: Release dates persisted.
        started_at: When the run began.
        finished_at: When the run ended.
    """

    results: tuple[SeriesIngestResult, ...]
    release_dates_written: int
    started_at: datetime
    finished_at: datetime

    @property
    def rows_written(self) -> int:
        """Total observations persisted across all series."""
        return sum(result.rows_written for result in self.results)

    @property
    def failures(self) -> tuple[SeriesIngestResult, ...]:
        """Series that failed, if any."""
        return tuple(result for result in self.results if not result.succeeded)

    @property
    def succeeded(self) -> bool:
        """True when every series ingested without error."""
        return not self.failures

    @property
    def duration_seconds(self) -> float:
        """Wall-clock duration of the run."""
        return (self.finished_at - self.started_at).total_seconds()


class IngestService:
    """Fetches every registered series and persists it with full revision history.

    Always ingests with ``all_vintages=True``. Current-vintage records report the
    fetch date as their vintage start and are display-only, so persisting them would
    break point-in-time reconstruction; the storage layer rejects them, and this
    service never produces them.
    """

    def __init__(
        self,
        source: IngestionPort,
        storage: StoragePort,
        calendar: ReleaseCalendarPort | None = None,
    ) -> None:
        """Wire the service to its collaborators.

        Args:
            source: Where observations come from.
            storage: Where they are persisted.
            calendar: Optional source of real release dates. When omitted, release
                dates are skipped — useful for adapters with no publication
                calendar, and the reason the two capabilities are separate protocols.
        """
        self._source = source
        self._storage = storage
        self._calendar = calendar

    def run(
        self,
        start: date | None = DEFAULT_START,
        *,
        series_ids: Sequence[str] | None = None,
        release_id: int = ADP_RELEASE_ID,
    ) -> IngestReport:
        """Ingest every requested series, then the release calendar.

        Idempotent and safe to re-run: writes upsert on the three-part vintage key, so
        a second run over the same range leaves the store in the same state.

        A series that fails is logged, checkpointed as failed by omission, and the run
        continues. One unavailable indicator should not cost the other six, and the
        report makes the partial failure explicit rather than silent.

        Args:
            start: Earliest reference period to fetch. ``None`` fetches full history.
            series_ids: Series to ingest. Defaults to the whole registry.
            release_id: Release whose publication dates to store.

        Returns:
            An :class:`IngestReport` describing what happened.
        """
        started_at = _now()
        targets = tuple(series_ids) if series_ids is not None else all_series_ids()

        _LOG.info(
            "Starting ingest of %d series from %s",
            len(targets),
            start.isoformat() if start else "beginning",
        )

        # its kinda double initialize we don't need to

#        self._storage.initialise()

        results = tuple(self._ingest_series(series_id, start) for series_id in targets)
        release_dates_written = self._ingest_release_dates(release_id)

        report = IngestReport(
            results=results,
            release_dates_written=release_dates_written,
            started_at=started_at,
            finished_at=_now(),
        )
        self._log_summary(report)
        return report

    # -- internals ---------------------------------------------------------

    def _ingest_series(self, series_id: str, start: date | None) -> SeriesIngestResult:
        """Fetch and persist one series, converting failure into a result."""
        try:
            observations = self._source.fetch(series_id, start, all_vintages=True)
        except AdpForecastError as exc:
            _LOG.error("Ingest failed for %s: %s: %s", series_id, type(exc).__name__, exc)
            return SeriesIngestResult(series_id, 0, None, error=exc)

        if not observations:
            _LOG.warning("%s returned no observations; nothing persisted", series_id)
            return SeriesIngestResult(series_id, 0, None)

        try:
            rows_written = self._storage.upsert_observations(observations)
        except AdpForecastError as exc:
            _LOG.error("Persist failed for %s: %s: %s", series_id, type(exc).__name__, exc)
            return SeriesIngestResult(series_id, 0, None, error=exc)

        max_obs_date = _max_observation_date(observations)
        self._storage.record_checkpoint(
            IngestCheckpoint(
                series_id=series_id,
                max_obs_date=max_obs_date,
                row_count=rows_written,
                completed_at=_now(),
            )
        )
        return SeriesIngestResult(series_id, rows_written, max_obs_date)

    def _ingest_release_dates(self, release_id: int) -> int:
        """Fetch and persist real release dates, if a calendar was provided."""
        if self._calendar is None:
            _LOG.debug("No release calendar configured; skipping release dates")
            return 0

        try:
            dates = self._calendar.fetch_release_dates(release_id)
        except AdpForecastError as exc:
            # Non-fatal: observations are already stored and usable. The backtest
            # needs these dates, but it is not what this run set out to produce.
            _LOG.error(
                "Could not fetch release dates for release_id=%d: %s: %s",
                release_id,
                type(exc).__name__,
                exc,
            )
            return 0

        return self._storage.upsert_release_dates(release_id, dates)

    @staticmethod
    def _log_summary(report: IngestReport) -> None:
        """Emit a one-line outcome, plus a line per failure."""
        _LOG.info(
            "Ingest finished in %.1fs: %d rows across %d series, %d release dates",
            report.duration_seconds,
            report.rows_written,
            len(report.results) - len(report.failures),
            report.release_dates_written,
        )
        for failure in report.failures:
            _LOG.error("  %s failed: %s", failure.series_id, failure.error)


def _now() -> datetime:
    """Current UTC time. Isolated so tests can patch a single seam."""
    return datetime.now(timezone.utc)


def _max_observation_date(observations: Sequence[Observation]) -> date | None:
    """Return the newest reference period in a batch, or ``None`` if empty.

    O(n) with no intermediate allocation, rather than sorting for a single extremum.
    """
    if not observations:
        return None
    return max(obs.date for obs in observations)
