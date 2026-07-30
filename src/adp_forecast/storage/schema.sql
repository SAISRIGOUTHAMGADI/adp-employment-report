-- Schema for the local observation store.
--
-- Applied idempotently on every connection: every statement is IF NOT EXISTS, so
-- opening an existing database is a no-op and a fresh clone self-initialises.
--
-- Dates are ISO-8601 TEXT ('YYYY-MM-DD'). SQLite has no date type, and ISO strings
-- sort and range-compare lexicographically, so the point-in-time predicate
--   realtime_start <= :as_of AND realtime_end >= :as_of
-- is an index range scan rather than a function call per row. Julian-day integers
-- would be marginally smaller and considerably harder to debug by hand.

PRAGMA journal_mode = WAL;      -- readers never block the ingest writer
PRAGMA synchronous = NORMAL;    -- WAL makes full fsync per commit unnecessary here
PRAGMA foreign_keys = ON;

-- One row per (series, reference period, vintage window).
--
-- The three-part primary key is the whole point of this design. A statistical agency
-- revising a number does not overwrite history: it closes one realtime window and
-- opens another. Keying on (series_id, obs_date) alone would collapse those into a
-- single mutable cell and make an honest backtest impossible.
--
-- realtime_end is deliberately NOT part of the key. When a revision lands, the
-- previously open window closes, so the upsert must UPDATE the existing row's
-- realtime_end from '9999-12-31' to a real date rather than insert a duplicate.
CREATE TABLE IF NOT EXISTS observations (
    series_id      TEXT    NOT NULL,
    obs_date       TEXT    NOT NULL,           -- reference period start
    realtime_start TEXT    NOT NULL,           -- first date this value was published
    realtime_end   TEXT    NOT NULL,           -- last such date; '9999-12-31' = current
    value          REAL,                       -- NULL when upstream published '.'
    source         TEXT    NOT NULL,           -- adapter that produced the row
    fetched_at     TEXT    NOT NULL,           -- our retrieval time (provenance)

    PRIMARY KEY (series_id, obs_date, realtime_start),

    -- Cheap invariants enforced by the engine rather than by convention.
    CHECK (obs_date       LIKE '____-__-__'),
    CHECK (realtime_start LIKE '____-__-__'),
    CHECK (realtime_end   LIKE '____-__-__'),
    CHECK (realtime_end >= realtime_start)
) WITHOUT ROWID;

-- WITHOUT ROWID above: the primary key IS the natural key, so storing an extra
-- implicit rowid would add a second B-tree and one indirection to every lookup.
--
-- No secondary index on the realtime columns, deliberately. A
-- (series_id, realtime_start, realtime_end) index was measured and dropped: because
-- this is a WITHOUT ROWID table, the primary key *is* the table, ordered
-- (series_id, obs_date, realtime_start). A `series_id = ?` seek therefore yields a
-- contiguous run already sorted in exactly the ORDER BY the point-in-time query
-- wants, so the realtime predicate is applied while scanning a range we must walk
-- anyway -- no sort, no second lookup. The index path would need an index scan plus
-- a PK lookup per row plus a sort, so SQLite never chose it even with ANALYZE
-- statistics present. Benchmarked over 6,160 as-of queries: identical runtime
-- (4487ms vs 4488ms) while costing 784 KB, a third of total database size, plus
-- write amplification on every ingest.
--
-- This holds because per-series row counts are small (5,693 at the largest). If a
-- series ever grows to where scanning its full history per as-of query dominates,
-- revisit with a covering index and re-measure -- do not add one on principle.

-- Real publication dates for a statistical release, used as backtest forecast
-- origins. Stored rather than recomputed because deriving them from a rule
-- ("first Wednesday") drifts around holidays, and an origin one day late leaks data
-- that did not exist yet -- a corruption that raises no error and flatters the score.
--
-- Note: FRED also returns *scheduled future* dates. They are persisted as-is; it is
-- the backtest's job to filter to dates that have already occurred.
CREATE TABLE IF NOT EXISTS release_dates (
    release_id   INTEGER NOT NULL,
    release_date TEXT    NOT NULL,

    PRIMARY KEY (release_id, release_date),
    CHECK (release_date LIKE '____-__-__')
) WITHOUT ROWID;
