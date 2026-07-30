"""Tests for the ``adp-forecast`` command-line interface.

Exercised through typer's runner against a real SQLite file, so these cover the wiring
end to end: storage open, service call, rendering, exit code. ``ingest`` is covered only
for argument validation, since running it would hit the network.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from adp_forecast.cli.app import app
from adp_forecast.config import TARGET_SERIES_ID, all_series_ids, get_series_spec
from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, Frequency, Observation
from adp_forecast.storage import SqliteStorage

runner = CliRunner()

FETCHED_AT = datetime(2026, 7, 30, tzinfo=timezone.utc)
FIRST_MONTH = date(2009, 6, 1)
MONTHS = 200


def _shift(value: date, offset: int) -> date:
    total = value.year * 12 + (value.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)


def _observation(series_id: str, obs_date: date, value: float, published: date):
    return Observation(
        series_id=series_id,
        date=obs_date,
        value=value,
        source="FRED",
        fetched_at=FETCHED_AT,
        realtime_start=published,
        realtime_end=CURRENT_VINTAGE_SENTINEL,
    )


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    """A populated database covering every registered series.

    Built once for the module: generating 200 months plus weekly claims per test would
    dominate the suite runtime for no additional coverage.
    """
    path = tmp_path_factory.mktemp("cli") / "adp.db"
    observations: list[Observation] = []

    level = 130_000_000.0
    for index in range(MONTHS):
        month = _shift(FIRST_MONTH, index)
        level += (100.0 + 20.0 * ((index % 7) - 3)) * 1_000.0
        observations.append(
            _observation(TARGET_SERIES_ID, month, level, _shift(month, 1))
        )

    for series_id in all_series_ids():
        spec = get_series_spec(series_id)
        if series_id == TARGET_SERIES_ID:
            continue
        if spec.frequency is Frequency.WEEKLY:
            week = date(2009, 6, 6)
            index = 0
            while week <= date(2026, 7, 25):
                observations.append(
                    _observation(
                        series_id,
                        week,
                        200_000.0 + 5_000.0 * ((index % 11) - 5),
                        week + timedelta(days=5),
                    )
                )
                week += timedelta(days=7)
                index += 1
            continue
        for index in range(MONTHS):
            month = _shift(FIRST_MONTH, index)
            published = _shift(month, spec.publication_lag_months)
            observations.append(
                _observation(
                    series_id, month, 200.0 + 5.0 * ((index % 11) - 5), published
                )
            )

    releases = [_shift(FIRST_MONTH, index + 1) for index in range(MONTHS)]

    with SqliteStorage(path) as storage:
        storage.initialise()
        storage.upsert_observations(observations)
        storage.upsert_release_dates(194, releases)
    return path


def invoke(database, *args):
    """Run the CLI against the fixture database."""
    return runner.invoke(app, ["--db", str(database), *args])


# -- structure -----------------------------------------------------------------


def test_help_lists_every_subcommand():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("ingest", "history", "forecast", "backtest"):
        assert command in result.stdout


def test_no_arguments_shows_help_rather_than_failing_silently():
    result = runner.invoke(app, [])

    assert "Usage" in result.stdout


@pytest.mark.parametrize("command", ["ingest", "history", "forecast", "backtest"])
def test_every_subcommand_documents_itself(command):
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0
    assert result.stdout.strip()


# -- history -------------------------------------------------------------------


def test_history_prints_recent_observations(database):
    result = invoke(database, "history", "-n", "4")

    assert result.exit_code == 0
    assert "ADP private payrolls" in result.stdout
    assert "MoM change" in result.stdout
    assert result.stdout.count("2026-") >= 1


def test_history_respects_the_count(database):
    short = invoke(database, "history", "-n", "3")
    long = invoke(database, "history", "-n", "10")

    assert short.stdout.count("\n") < long.stdout.count("\n")


def test_history_accepts_any_registered_series(database):
    result = invoke(database, "history", "--series", "ICSA", "-n", "3")

    assert result.exit_code == 0
    assert "Initial jobless claims" in result.stdout


def test_history_rejects_an_unknown_series(database):
    result = invoke(database, "history", "--series", "NOPE")

    assert result.exit_code == 2
    assert "Unknown series" in result.output


def test_history_rejects_a_zero_count(database):
    assert invoke(database, "history", "-n", "0").exit_code != 0


# -- forecast ------------------------------------------------------------------


def test_forecast_prints_a_prediction_and_reasoning(database):
    result = invoke(database, "forecast")

    assert result.exit_code == 0
    assert "forecast to report" in result.stdout
    assert "Why:" in result.stdout
    assert "Caveats:" in result.stdout


def test_forecast_json_is_valid_and_carries_structured_drivers(database):
    """The payload an HTTP layer would return, with no prose reparsing."""
    result = invoke(database, "forecast", "--json")

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["series_id"] == TARGET_SERIES_ID
    assert payload["model"] == "ridge"
    assert isinstance(payload["point_thousands"], float)
    assert payload["drivers"]
    assert {"name", "label", "contribution"} <= set(payload["drivers"][0])


def test_json_and_text_describe_the_same_forecast(database):
    """Two renderings of one typed object; they must not diverge."""
    text = invoke(database, "forecast").stdout
    payload = json.loads(invoke(database, "forecast", "--json").stdout)

    assert payload["headline"] in text


def test_forecast_driver_count_is_configurable(database):
    few = invoke(database, "forecast", "--drivers", "1").stdout
    many = invoke(database, "forecast", "--drivers", "5").stdout

    assert few.count("which") < many.count("which")


@pytest.mark.parametrize("model", ["ridge", "random_walk", "mean_3m", "drift"])
def test_every_registered_model_is_selectable(database, model):
    result = invoke(database, "forecast", "--model", model)

    assert result.exit_code == 0
    assert "forecast to report" in result.stdout


def test_forecast_rejects_an_unknown_model(database):
    result = invoke(database, "forecast", "--model", "bogus")

    assert result.exit_code == 2
    assert "Unknown model" in result.output


# -- backtest ------------------------------------------------------------------


def test_backtest_prints_the_vintage_scorecard(database):
    result = invoke(database, "backtest", "--scorecard", "vintage")

    assert result.exit_code == 0
    assert "VINTAGE-CORRECT SCORECARD" in result.stdout
    assert "best MAE" in result.stdout
    assert "ridge" in result.stdout


def test_backtest_reports_dropped_origins(database):
    """Silent truncation would read as full coverage."""
    result = invoke(database, "backtest", "--scorecard", "vintage")

    assert "dropped:" in result.stdout


def test_backtest_rejects_an_unknown_scorecard(database):
    result = invoke(database, "backtest", "--scorecard", "nope")

    assert result.exit_code == 2
    assert "Unknown scorecard" in result.output


def test_backtest_rejects_an_impossible_interval_level(database):
    assert invoke(database, "backtest", "--interval-level", "1.5").exit_code != 0


# -- ingest argument validation ------------------------------------------------


def test_ingest_rejects_an_unknown_series(database):
    """Validated before any network call, so a typo fails immediately."""
    result = invoke(database, "ingest", "--series", "NOPE")

    assert result.exit_code == 2
    assert "Unknown series" in result.output


def test_ingest_rejects_a_malformed_start_date(database):
    result = invoke(database, "ingest", "--start", "July 2026")

    assert result.exit_code == 2
    assert "Invalid date" in result.output


# -- shared failure handling ---------------------------------------------------


def test_missing_database_directs_the_user_to_ingest(tmp_path):
    result = runner.invoke(
        app, ["--db", str(tmp_path / "absent.db"), "forecast"]
    )

    assert result.exit_code == 1
    assert "Run `adp-forecast ingest` first" in result.output


def test_missing_database_is_reported_the_same_way_for_every_read_command(tmp_path):
    for command in ("history", "forecast", "backtest"):
        result = runner.invoke(app, ["--db", str(tmp_path / "absent.db"), command])
        assert result.exit_code == 1, command
        assert "adp-forecast ingest" in result.output, command


def test_expected_failures_do_not_print_a_traceback(tmp_path):
    """A stack trace for a missing database is noise, not information."""
    result = runner.invoke(app, ["--db", str(tmp_path / "absent.db"), "forecast"])

    assert "Traceback" not in result.output


def test_db_option_selects_the_database(database, tmp_path):
    populated = invoke(database, "history", "-n", "2")
    empty = runner.invoke(app, ["--db", str(tmp_path / "other.db"), "history"])

    assert populated.exit_code == 0
    assert empty.exit_code == 1
