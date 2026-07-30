"""Unit tests for the SQLite storage adapter.

Runs against ``:memory:`` databases, so the suite stays offline and fast. The
point-in-time read and the vintage-batch guard are the two behaviours worth the most
scrutiny: the first is what makes the backtest honest, the second is what stops
display-only records from silently corrupting it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, Observation
from adp_forecast.exceptions import StorageIntegrityError, VintageValidationError
from adp_forecast.storage import IngestCheckpoint, SqliteStorage, StoragePort

FETCHED_AT = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    """An initialised in-memory store."""
    with SqliteStorage(":memory:") as instance:
        instance.initialise()
        yield instance


def obs(
    series_id: str = "USPRIV",
    obs_date: date = date(2026, 4, 1),
    value: float | None = 135_428.0,
    realtime_start: date = date(2026, 5, 8),
    realtime_end: date = date(2026, 6, 4),
    fetched_at: datetime = FETCHED_AT,
) -> Observation:
    """Build an observation, defaulting to USPRIV April 2026's first print."""
    return Observation(
        series_id=series_id,
        date=obs_date,
        value=value,
        source="FRED",
        fetched_at=fetched_at,
        realtime_start=realtime_start,
        realtime_end=realtime_end,
    )


#: USPRIV April 2026's three real vintages, as verified against the live API.
APRIL_VINTAGES = [
    obs(value=135_428.0, realtime_start=date(2026, 5, 8), realtime_end=date(2026, 6, 4)),
    obs(value=135_494.0, realtime_start=date(2026, 6, 5), realtime_end=date(2026, 7, 1)),
    obs(
        value=135_467.0,
        realtime_start=date(2026, 7, 2),
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    ),
]


# -- contract ------------------------------------------------------------------


def test_adapter_satisfies_the_storage_port(store):
    assert isinstance(store, StoragePort)


def test_initialise_is_idempotent(store):
    store.initialise()
    store.initialise()

    assert store.count_observations() == 0


# -- writes --------------------------------------------------------------------


def test_upsert_persists_and_reads_back(store):
    assert store.upsert_observations(APRIL_VINTAGES) == 3

    stored = store.read_observations("USPRIV")
    assert len(stored) == 3
    assert [record.value for record in stored] == [135_428.0, 135_494.0, 135_467.0]


def test_empty_batch_is_a_no_op(store):
    assert store.upsert_observations([]) == 0
    assert store.count_observations() == 0


def test_reingest_is_idempotent(store):
    """Re-running ingest must not duplicate rows — the whole point of the upsert key."""
    store.upsert_observations(APRIL_VINTAGES)
    store.upsert_observations(APRIL_VINTAGES)

    assert store.count_observations("USPRIV") == 3


def test_revision_closes_the_open_window_in_place(store):
    """A superseded window must UPDATE, not insert a duplicate row."""
    open_window = obs(
        value=135_467.0,
        realtime_start=date(2026, 7, 2),
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    )
    store.upsert_observations([APRIL_VINTAGES[0], open_window])
    assert store.count_observations("USPRIV") == 2

    # A later ingest sees that window closed by a new revision.
    closed = obs(
        value=135_467.0,
        realtime_start=date(2026, 7, 2),
        realtime_end=date(2026, 8, 5),
    )
    store.upsert_observations([APRIL_VINTAGES[0], closed])

    stored = store.read_observations("USPRIV")
    assert store.count_observations("USPRIV") == 2, "must update, not append"
    assert stored[-1].realtime_end == date(2026, 8, 5)
    assert not stored[-1].is_current_vintage


def test_missing_value_round_trips_as_none(store):
    store.upsert_observations([obs(series_id="ICSA", value=None)])

    stored = store.read_observations("ICSA")
    assert stored[0].value is None
    assert stored[0].is_missing


def test_series_are_isolated_from_each_other(store):
    store.upsert_observations(
        [obs(series_id="USPRIV"), obs(series_id="PAYEMS", value=158_984.0)]
    )

    assert len(store.read_observations("USPRIV")) == 1
    assert len(store.read_observations("PAYEMS")) == 1
    assert store.count_observations() == 2


def test_large_batch_spans_multiple_chunks(store):
    """Exercises the chunking path past _BATCH_SIZE without a huge fixture."""
    from adp_forecast.storage import sqlite as sqlite_module

    original = sqlite_module._BATCH_SIZE
    sqlite_module._BATCH_SIZE = 10
    try:
        batch = [
            obs(obs_date=date(2020, 1, 1), realtime_start=date(2020, 2, 1)),
            *[
                obs(
                    obs_date=date(2024, 1, 1),
                    realtime_start=date(2024, 2, 1 + index),
                    realtime_end=date(2024, 3, 1 + index),
                    value=float(index),
                )
                for index in range(25)
            ],
        ]
        assert store.upsert_observations(batch) == 26
    finally:
        sqlite_module._BATCH_SIZE = original

    assert store.count_observations("USPRIV") == 26


# -- point-in-time reads -------------------------------------------------------


def test_as_of_returns_the_vintage_current_on_that_date(store):
    """The backtest's core guarantee, at the storage boundary."""
    store.upsert_observations(APRIL_VINTAGES)

    stored = store.read_observations("USPRIV", as_of=date(2026, 5, 20))

    assert len(stored) == 1, "exactly one vintage was current on 2026-05-20"
    assert stored[0].value == pytest.approx(135_428.0)


@pytest.mark.parametrize(
    "as_of, expected",
    [
        (date(2026, 5, 7), None),        # before first publication
        (date(2026, 5, 8), 135_428.0),   # inclusive lower bound
        (date(2026, 6, 4), 135_428.0),   # inclusive upper bound
        (date(2026, 6, 5), 135_494.0),   # first revision
        (date(2026, 7, 2), 135_467.0),   # second revision
        (date(2099, 1, 1), 135_467.0),   # open window extends indefinitely
    ],
)
def test_as_of_boundaries_are_inclusive(store, as_of, expected):
    """Off-by-one here leaks or drops a day of data in every backtest origin."""
    store.upsert_observations(APRIL_VINTAGES)

    stored = store.read_observations("USPRIV", as_of=as_of)

    if expected is None:
        assert stored == []
    else:
        assert len(stored) == 1
        assert stored[0].value == pytest.approx(expected)


def test_as_of_yields_at_most_one_row_per_reference_period(store):
    """Overlapping windows would make a point-in-time read ambiguous."""
    store.upsert_observations(
        APRIL_VINTAGES
        + [
            obs(
                obs_date=date(2026, 5, 1),
                value=135_545.0,
                realtime_start=date(2026, 6, 5),
                realtime_end=CURRENT_VINTAGE_SENTINEL,
            )
        ]
    )

    stored = store.read_observations("USPRIV", as_of=date(2026, 6, 20))
    dates = [record.date for record in stored]

    assert len(dates) == len(set(dates))
    assert set(dates) == {date(2026, 4, 1), date(2026, 5, 1)}


def test_as_of_excludes_periods_not_yet_published(store):
    """A reference period published later must be invisible at an earlier origin."""
    store.upsert_observations(
        APRIL_VINTAGES
        + [
            obs(
                obs_date=date(2026, 5, 1),
                value=135_545.0,
                realtime_start=date(2026, 7, 2),
                realtime_end=CURRENT_VINTAGE_SENTINEL,
            )
        ]
    )

    stored = store.read_observations("USPRIV", as_of=date(2026, 5, 20))

    assert [record.date for record in stored] == [date(2026, 4, 1)]


def test_start_filters_by_reference_period(store):
    store.upsert_observations(
        [
            obs(obs_date=date(2024, 1, 1), realtime_start=date(2024, 2, 1)),
            obs(obs_date=date(2026, 4, 1), realtime_start=date(2026, 5, 8)),
        ]
    )

    stored = store.read_observations("USPRIV", start=date(2025, 1, 1))

    assert [record.date for record in stored] == [date(2026, 4, 1)]


def test_reads_are_ordered_by_date_then_vintage(store):
    store.upsert_observations(list(reversed(APRIL_VINTAGES)))

    stored = store.read_observations("USPRIV")
    keys = [(record.date, record.realtime_start) for record in stored]

    assert keys == sorted(keys)


def test_read_of_unknown_series_returns_empty(store):
    assert store.read_observations("NOPE") == []


# -- vintage-batch guard -------------------------------------------------------


def test_display_only_batch_is_rejected(store):
    """Every realtime_start equal to the fetch date is a current-vintage fetch."""
    today = FETCHED_AT.date()
    display_only = [
        obs(
            obs_date=date(2026, 5, 1),
            realtime_start=today,
            realtime_end=CURRENT_VINTAGE_SENTINEL,
        ),
        obs(
            obs_date=date(2026, 6, 1),
            realtime_start=today,
            realtime_end=CURRENT_VINTAGE_SENTINEL,
        ),
    ]

    with pytest.raises(VintageValidationError, match="all_vintages=True"):
        store.upsert_observations(display_only)

    assert store.count_observations() == 0, "rejected batch must not partially write"


def test_genuine_vintage_batch_is_accepted(store):
    """Real history carries at least one realtime_start before the fetch date."""
    assert store.upsert_observations(APRIL_VINTAGES) == 3


def test_batch_with_one_historical_vintage_is_accepted(store):
    """A mostly-current batch is fine as long as real history is present."""
    today = FETCHED_AT.date()
    batch = [
        obs(realtime_start=date(2026, 5, 8), realtime_end=date(2026, 6, 4)),
        obs(
            obs_date=date(2026, 6, 1),
            realtime_start=today,
            realtime_end=CURRENT_VINTAGE_SENTINEL,
        ),
    ]

    assert store.upsert_observations(batch) == 2


def test_inverted_vintage_window_is_rejected(store):
    inverted = obs(realtime_start=date(2026, 6, 4), realtime_end=date(2026, 5, 8))

    with pytest.raises(VintageValidationError, match="precedes"):
        store.upsert_observations([inverted])


def test_schema_check_constraint_rejects_a_malformed_date(store):
    """Defence in depth: the engine enforces the date shape independently."""
    store.upsert_observations(APRIL_VINTAGES)

    with pytest.raises(StorageIntegrityError):
        store._connection.execute("BEGIN")
        try:
            store._connection.execute(
                "INSERT INTO observations VALUES "
                "('USPRIV', 'nope', '2026-05-08', '9999-12-31', 1.0, 'FRED', 'x')"
            )
        except Exception as exc:
            store._connection.execute("ROLLBACK")
            raise StorageIntegrityError(str(exc)) from exc


# -- release dates -------------------------------------------------------------


def test_release_dates_round_trip_and_deduplicate(store):
    dates = [date(2026, 5, 6), date(2026, 6, 3), date(2026, 7, 1)]

    assert store.upsert_release_dates(194, dates) == 3
    store.upsert_release_dates(194, dates)

    assert store.read_release_dates(194) == dates


def test_release_dates_are_returned_ascending(store):
    store.upsert_release_dates(194, [date(2026, 7, 1), date(2026, 5, 6)])

    assert store.read_release_dates(194) == [date(2026, 5, 6), date(2026, 7, 1)]


def test_through_excludes_scheduled_future_dates(store):
    """FRED returns future release dates; the backtest must be able to exclude them."""
    store.upsert_release_dates(
        194, [date(2026, 6, 3), date(2026, 7, 1), date(2026, 12, 2)]
    )

    past = store.read_release_dates(194, through=date(2026, 7, 30))

    assert past == [date(2026, 6, 3), date(2026, 7, 1)]


def test_empty_release_date_list_is_a_no_op(store):
    assert store.upsert_release_dates(194, []) == 0
    assert store.read_release_dates(194) == []


def test_releases_are_isolated_from_each_other(store):
    store.upsert_release_dates(194, [date(2026, 7, 1)])
    store.upsert_release_dates(50, [date(2026, 7, 2)])

    assert store.read_release_dates(194) == [date(2026, 7, 1)]
    assert store.read_release_dates(50) == [date(2026, 7, 2)]


# -- checkpoints ---------------------------------------------------------------


def test_checkpoint_round_trips(store):
    checkpoint = IngestCheckpoint(
        series_id="USPRIV",
        max_obs_date=date(2026, 6, 1),
        row_count=2082,
        completed_at=FETCHED_AT,
    )
    store.record_checkpoint(checkpoint)

    stored = store.read_checkpoint("USPRIV")
    assert stored == checkpoint


def test_checkpoint_is_overwritten_not_duplicated(store):
    """One checkpoint per series: keyed on series_id alone, by design."""
    store.record_checkpoint(IngestCheckpoint("USPRIV", date(2026, 5, 1), 10, FETCHED_AT))
    store.record_checkpoint(IngestCheckpoint("USPRIV", date(2026, 6, 1), 20, FETCHED_AT))

    stored = store.read_checkpoint("USPRIV")
    assert stored is not None
    assert stored.row_count == 20
    assert stored.max_obs_date == date(2026, 6, 1)


def test_checkpoint_tolerates_a_series_with_no_observations(store):
    store.record_checkpoint(IngestCheckpoint("USPRIV", None, 0, FETCHED_AT))

    stored = store.read_checkpoint("USPRIV")
    assert stored is not None
    assert stored.max_obs_date is None


def test_unknown_checkpoint_is_none(store):
    assert store.read_checkpoint("NEVER_INGESTED") is None


# -- persistence ---------------------------------------------------------------


def test_data_survives_reopening_the_file(tmp_path):
    """Confirms the schema and writes actually reach disk, not just a memory db."""
    db_path = tmp_path / "nested" / "adp.db"

    with SqliteStorage(db_path) as store:
        store.initialise()
        store.upsert_observations(APRIL_VINTAGES)

    with SqliteStorage(db_path) as reopened:
        reopened.initialise()
        assert reopened.count_observations("USPRIV") == 3
        assert reopened.read_observations("USPRIV", as_of=date(2026, 5, 20))[
            0
        ].value == pytest.approx(135_428.0)


def test_parent_directories_are_created(tmp_path):
    db_path = tmp_path / "a" / "b" / "c" / "adp.db"

    with SqliteStorage(db_path) as store:
        store.initialise()

    assert db_path.exists()
