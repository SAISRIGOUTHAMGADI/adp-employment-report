"""Unit tests for :class:`adp_forecast.pipeline.IngestService`.

Both collaborators are protocols, so the whole pipeline is exercised here with a fake
source and a real in-memory store — no network, no files. That is the payoff of
depending on contracts rather than on adapters.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.config import ADP_RELEASE_ID, TARGET_SERIES_ID, all_series_ids
from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, Observation
from adp_forecast.exceptions import SeriesNotFoundError, TransientIngestionError
from adp_forecast.ingestion.port import IngestionPort, ReleaseCalendarPort
from adp_forecast.pipeline import IngestService
from adp_forecast.storage import SqliteStorage

FETCHED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


class FakeSource:
    """Scripted :class:`IngestionPort` / :class:`ReleaseCalendarPort` double.

    Records the ``all_vintages`` flag it was called with, which is how the tests
    assert that the service never requests display-only data.
    """

    source_name = "FAKE"

    def __init__(
        self,
        observations: dict[str, list[Observation]] | None = None,
        release_dates: list[date] | None = None,
        errors: dict[str, Exception] | None = None,
        calendar_error: Exception | None = None,
    ) -> None:
        self._observations = observations or {}
        self._release_dates = release_dates or []
        self._errors = errors or {}
        self._calendar_error = calendar_error
        self.fetch_calls: list[tuple[str, date | None, bool]] = []
        self.calendar_calls: list[int] = []

    def fetch(
        self,
        series_id: str,
        start: date | None = None,
        *,
        all_vintages: bool = False,
    ) -> list[Observation]:
        self.fetch_calls.append((series_id, start, all_vintages))
        if series_id in self._errors:
            raise self._errors[series_id]
        return list(self._observations.get(series_id, []))

    def fetch_release_dates(
        self, release_id: int, start: date | None = None
    ) -> list[date]:
        self.calendar_calls.append(release_id)
        if self._calendar_error is not None:
            raise self._calendar_error
        return list(self._release_dates)


def vintage_observation(
    series_id: str,
    obs_date: date = date(2026, 4, 1),
    value: float = 1.0,
    realtime_start: date = date(2026, 5, 8),
    realtime_end: date = CURRENT_VINTAGE_SENTINEL,
) -> Observation:
    """Build an observation with a genuine historical vintage window."""
    return Observation(
        series_id=series_id,
        date=obs_date,
        value=value,
        source="FAKE",
        fetched_at=FETCHED_AT,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )


@pytest.fixture
def store():
    """An initialised in-memory store.

    IngestService no longer applies the schema itself, so the caller owns that step,
    exactly as the CLI does in `_with_storage()` before constructing the service.
    """
    with SqliteStorage(":memory:") as instance:
        instance.initialise()
        yield instance


@pytest.fixture
def two_series():
    """Observations for two series, each with real vintage history."""
    return {
        TARGET_SERIES_ID: [
            vintage_observation(TARGET_SERIES_ID, date(2026, 5, 1), 132_624_000.0),
            vintage_observation(TARGET_SERIES_ID, date(2026, 6, 1), 132_722_000.0),
        ],
        "USPRIV": [vintage_observation("USPRIV", date(2026, 4, 1), 135_428.0)],
    }


# -- contract ------------------------------------------------------------------


def test_fake_source_satisfies_both_ports():
    """If the double drifts from the protocols, these tests stop proving anything."""
    source = FakeSource()
    assert isinstance(source, IngestionPort)
    assert isinstance(source, ReleaseCalendarPort)


# -- happy path ----------------------------------------------------------------


def test_run_persists_every_series(store, two_series):
    source = FakeSource(observations=two_series)
    service = IngestService(source, store, calendar=source)

    report = service.run(series_ids=[TARGET_SERIES_ID, "USPRIV"])

    assert report.succeeded
    assert report.rows_written == 3
    assert store.count_observations(TARGET_SERIES_ID) == 2
    assert store.count_observations("USPRIV") == 1


def test_run_requires_an_initialised_store(two_series):
    """The service assumes a ready schema; it does not apply DDL itself.

    Pins the contract so the responsibility stays with the caller rather than drifting
    back into the service.
    """
    source = FakeSource(observations=two_series)

    with SqliteStorage(":memory:") as bare:
        report = IngestService(source, bare).run(series_ids=[TARGET_SERIES_ID])

        assert not report.succeeded
        assert "no such table" in str(report.failures[0].error)


def test_run_persists_into_an_initialised_store(store, two_series):
    source = FakeSource(observations=two_series)

    IngestService(source, store).run(series_ids=[TARGET_SERIES_ID])

    assert store.count_observations() == 2


def test_run_always_requests_full_vintage_history(store, two_series):
    """The service must never fetch display-only records. This is the guarantee."""
    source = FakeSource(observations=two_series)

    IngestService(source, store).run(series_ids=[TARGET_SERIES_ID, "USPRIV"])

    assert all(all_vintages for _sid, _start, all_vintages in source.fetch_calls)


def test_run_defaults_to_the_whole_registry(store):
    source = FakeSource(
        observations={sid: [vintage_observation(sid)] for sid in all_series_ids()}
    )

    report = IngestService(source, store).run()

    assert [result.series_id for result in report.results] == list(all_series_ids())
    assert report.rows_written == len(all_series_ids())


def test_start_date_is_passed_through(store, two_series):
    source = FakeSource(observations=two_series)

    IngestService(source, store).run(date(2015, 1, 1), series_ids=[TARGET_SERIES_ID])

    assert source.fetch_calls[0][1] == date(2015, 1, 1)


def test_run_is_idempotent(store, two_series):
    source = FakeSource(observations=two_series)
    service = IngestService(source, store, calendar=source)

    service.run(series_ids=[TARGET_SERIES_ID])
    service.run(series_ids=[TARGET_SERIES_ID])

    assert store.count_observations(TARGET_SERIES_ID) == 2


# -- partial failure -----------------------------------------------------------


def test_one_failing_series_does_not_abort_the_run(store, two_series):
    """Six good indicators should not be lost to one bad one."""
    source = FakeSource(
        observations=two_series,
        errors={TARGET_SERIES_ID: SeriesNotFoundError("typo")},
    )

    report = IngestService(source, store).run(series_ids=[TARGET_SERIES_ID, "USPRIV"])

    assert not report.succeeded
    assert len(report.failures) == 1
    assert report.failures[0].series_id == TARGET_SERIES_ID
    assert store.count_observations("USPRIV") == 1, "the good series still landed"


def test_failure_is_reported_not_raised(store):
    source = FakeSource(errors={TARGET_SERIES_ID: TransientIngestionError("down")})

    report = IngestService(source, store).run(series_ids=[TARGET_SERIES_ID])

    assert isinstance(report.failures[0].error, TransientIngestionError)
    assert report.rows_written == 0


def test_empty_series_response_is_not_a_failure(store):
    """No data is a legitimate answer; it is not an error, and stores nothing."""
    source = FakeSource(observations={TARGET_SERIES_ID: []})

    report = IngestService(source, store).run(series_ids=[TARGET_SERIES_ID])

    assert report.succeeded
    assert report.rows_written == 0

# -- release calendar ----------------------------------------------------------


def test_release_dates_are_persisted(store, two_series):
    dates = [date(2026, 6, 3), date(2026, 7, 1)]
    source = FakeSource(observations=two_series, release_dates=dates)

    report = IngestService(source, store, calendar=source).run(
        series_ids=[TARGET_SERIES_ID]
    )

    assert report.release_dates_written == 2
    assert store.read_release_dates(ADP_RELEASE_ID) == dates


def test_missing_calendar_is_skipped_cleanly(store, two_series):
    """An adapter with no publication calendar must still be usable for observations."""
    source = FakeSource(observations=two_series)

    report = IngestService(source, store, calendar=None).run(
        series_ids=[TARGET_SERIES_ID]
    )

    assert report.succeeded
    assert report.release_dates_written == 0
    assert source.calendar_calls == []


def test_calendar_failure_does_not_fail_the_run(store, two_series):
    """Observations are already stored and useful; the calendar is a bonus."""
    source = FakeSource(
        observations=two_series,
        calendar_error=TransientIngestionError("calendar down"),
    )

    report = IngestService(source, store, calendar=source).run(
        series_ids=[TARGET_SERIES_ID]
    )

    assert report.succeeded
    assert report.release_dates_written == 0
    assert store.count_observations(TARGET_SERIES_ID) == 2


# -- report --------------------------------------------------------------------


def test_report_aggregates_totals_and_duration(store, two_series):
    source = FakeSource(observations=two_series, release_dates=[date(2026, 7, 1)])

    report = IngestService(source, store, calendar=source).run(
        series_ids=[TARGET_SERIES_ID, "USPRIV"]
    )

    assert report.rows_written == 3
    assert report.release_dates_written == 1
    assert report.duration_seconds >= 0.0
    assert report.finished_at >= report.started_at


def test_results_preserve_requested_order(store):
    source = FakeSource(
        observations={sid: [vintage_observation(sid)] for sid in ("USPRIV", "ICSA")}
    )

    report = IngestService(source, store).run(series_ids=["USPRIV", "ICSA"])

    assert [result.series_id for result in report.results] == ["USPRIV", "ICSA"]


# -- end-to-end read-back ------------------------------------------------------


def test_ingested_data_supports_point_in_time_reads(store):
    """The pipeline's purpose: what lands must be queryable as-of a past date."""
    source = FakeSource(
        observations={
            "USPRIV": [
                vintage_observation(
                    "USPRIV",
                    date(2026, 4, 1),
                    135_428.0,
                    realtime_start=date(2026, 5, 8),
                    realtime_end=date(2026, 6, 4),
                ),
                vintage_observation(
                    "USPRIV",
                    date(2026, 4, 1),
                    135_467.0,
                    realtime_start=date(2026, 6, 5),
                ),
            ]
        }
    )

    IngestService(source, store).run(series_ids=["USPRIV"])

    on_may_20 = store.read_observations("USPRIV", as_of=date(2026, 5, 20))
    today = store.read_observations("USPRIV", as_of=date(2026, 7, 30))

    assert on_may_20[0].value == pytest.approx(135_428.0)
    assert today[0].value == pytest.approx(135_467.0)
