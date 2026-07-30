"""The ``adp-forecast`` command-line interface.

One entry point with four subcommands: ``ingest``, ``history``, ``forecast`` and
``backtest``.

This module is deliberately thin. Every command opens storage, calls a service object
that returns a typed dataclass, and hands that object to
:mod:`adp_forecast.cli.render`. No command computes anything itself — which is what
keeps a FastAPI shim a small addition rather than a rewrite, and is why ``forecast``
can emit JSON without any duplicated logic.

Failure handling is uniform: anything deriving from
:class:`~adp_forecast.exceptions.AdpForecastError` is an expected condition (missing key,
unknown series, upstream down, too little history) and is reported as a clean message
with exit code 1. A traceback would only be noise for those.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import typer

from ..config import (
    ADP_RELEASE_ID,
    TARGET_SERIES_ID,
    FredSettings,
    all_series_ids,
    get_series_spec,
)
from ..evaluation import Backtester, Scorecard
from ..exceptions import AdpForecastError
from ..explanation import explain_forecast
from ..features import FeaturePanelBuilder
from ..forecast import DEFAULT_MODEL, MODEL_REGISTRY, get_model
from ..ingestion import FredAdapter
from ..logging_config import configure_logging, get_logger
from ..pipeline import DEFAULT_START, IngestService
from ..storage import SqliteStorage
from . import render

_LOG = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/adp.db")

#: Baseline the explanation's accuracy caveat compares against. The strongest naive
#: rule in the backtest, so the comparison is honest rather than flattering.
COMPARISON_BASELINE = "mean_3m"

app = typer.Typer(
    name="adp-forecast",
    help="Track and forecast the ADP National Employment Report.",
    add_completion=False,
    no_args_is_help=True,
)

_state: dict[str, object] = {"db": DEFAULT_DB_PATH}


@app.callback()
def main_options(
    db: Path = typer.Option(
        DEFAULT_DB_PATH, "--db", help="SQLite database path.", show_default=True
    ),
    log_level: str = typer.Option(
        "WARNING", "--log-level", help="DEBUG, INFO, WARNING or ERROR."
    ),
) -> None:
    """Options shared by every subcommand."""
    configure_logging(log_level)
    _state["db"] = db


@app.command()
def ingest(
    start: Optional[str] = typer.Option(
        None, "--start", help="Earliest reference period, YYYY-MM-DD."
    ),
    series: Optional[list[str]] = typer.Option(
        None, "--series", help="Series to ingest. Repeatable. Defaults to all."
    ),
) -> None:
    """Fetch every tracked series from FRED with full revision history.

    Idempotent: re-running upserts on the vintage key and closes any window a revision
    has superseded. There is deliberately no incremental mode — a full re-ingest costs
    about two seconds, and a cutoff would miss a revision to an older observation
    arriving after it.
    """
    database = _database()
    start_date = _parse_date(start) if start else DEFAULT_START
    targets = _validated_series(series)

    def run(storage: SqliteStorage) -> int:
        with FredAdapter(FredSettings.from_env()) as adapter:
            service = IngestService(adapter, storage, calendar=adapter)
            report = service.run(
                start_date, series_ids=targets, release_id=ADP_RELEASE_ID
            )
        typer.echo(
            "\n" + render.render_ingest(report, database, storage.count_observations())
        )
        return 0 if report.succeeded else 1

    raise typer.Exit(_with_storage(run, require_existing=False))


@app.command()
def history(
    series: str = typer.Option(
        TARGET_SERIES_ID, "--series", help="Registered series to display."
    ),
    count: int = typer.Option(12, "--count", "-n", min=1, help="Rows to show."),
) -> None:
    """Show recent published values and their month-over-month changes."""
    spec = _spec(series)

    def run(storage: SqliteStorage) -> int:
        observations = storage.read_observations(spec.series_id, as_of=date.today())
        if not observations:
            typer.echo(
                f"No observations for {spec.series_id}. Run `adp-forecast ingest`.",
                err=True,
            )
            return 1
        typer.echo(
            "\n"
            + render.render_history(observations, spec.series_id, spec.label, count)
        )
        return 0

    raise typer.Exit(_with_storage(run))


@app.command()
def forecast(
    model: str = typer.Option(
        DEFAULT_MODEL, "--model", "-m", help="Model to use."
    ),
    drivers: int = typer.Option(3, "--drivers", min=1, help="Drivers to name."),
    with_accuracy: bool = typer.Option(
        False,
        "--with-accuracy",
        help="Run the backtest first so caveats quote measured accuracy.",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of prose."
    ),
) -> None:
    """Forecast the next ADP print and explain the reasoning behind it."""
    if model not in MODEL_REGISTRY:
        typer.echo(
            f"Unknown model '{model}'. Available: {', '.join(sorted(MODEL_REGISTRY))}",
            err=True,
        )
        raise typer.Exit(2)

    def run(storage: SqliteStorage) -> int:
        panel = FeaturePanelBuilder(storage).build(date.today())
        result = get_model(model).forecast(panel)

        accuracy = baseline_accuracy = None
        if with_accuracy:
            report = Backtester(storage).run(
                Scorecard.VINTAGE, models=(model, COMPARISON_BASELINE)
            )
            accuracy = report.scores[model]
            baseline_accuracy = report.scores[COMPARISON_BASELINE]

        explanation = explain_forecast(
            result,
            panel,
            driver_count=drivers,
            accuracy=accuracy,
            baseline_accuracy=baseline_accuracy,
        )
        if as_json:
            typer.echo(render.forecast_as_json(result, explanation))
        else:
            typer.echo("\n" + render.render_explanation(explanation))
        return 0

    raise typer.Exit(_with_storage(run))


@app.command()
def backtest(
    scorecard: str = typer.Option(
        "both", "--scorecard", help="vintage, lag_shifted or both."
    ),
    interval_level: float = typer.Option(
        0.80, "--interval-level", min=0.01, max=0.99, help="Nominal coverage."
    ),
) -> None:
    """Walk-forward backtest of every model against the naive baselines.

    Reproduces the accuracy figures quoted in the README.
    """
    if scorecard not in {"vintage", "lag_shifted", "both"}:
        typer.echo(
            f"Unknown scorecard '{scorecard}'. Use vintage, lag_shifted or both.",
            err=True,
        )
        raise typer.Exit(2)

    wanted = (
        [Scorecard.VINTAGE, Scorecard.LAG_SHIFTED]
        if scorecard == "both"
        else [Scorecard(scorecard)]
    )

    def run(storage: SqliteStorage) -> int:
        backtester = Backtester(storage)
        for card in wanted:
            report = backtester.run(card, interval_level=interval_level)
            typer.echo("\n" + render.render_backtest(report))
        return 0

    raise typer.Exit(_with_storage(run))


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _database() -> Path:
    """Return the database path selected by the global option."""
    return _state["db"]  # type: ignore[return-value]


def _with_storage(command, *, require_existing: bool = True) -> int:
    """Open storage, run a command, and translate expected failures into exit codes.

    Centralised so every subcommand handles a missing database, a missing API key or an
    upstream outage identically, and so no command grows its own error handling.

    Args:
        command: Callable taking an open :class:`SqliteStorage` and returning an exit
            code.
        require_existing: When true, a missing database file is an error directing the
            user to ingest first, rather than silently creating an empty one.
    """
    database = _database()
    if require_existing and not database.exists():
        typer.echo(
            f"No database at {database}. Run `adp-forecast ingest` first.", err=True
        )
        return 1

    try:
        with SqliteStorage(database) as storage:
            storage.initialise()
            return command(storage)
    except AdpForecastError as exc:
        typer.echo(f"{type(exc).__name__}: {exc}", err=True)
        return 1


def _spec(series_id: str):
    """Resolve a registered series, exiting cleanly on a typo."""
    try:
        return get_series_spec(series_id)
    except AdpForecastError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None


def _validated_series(series: list[str] | None) -> list[str] | None:
    """Validate requested series before any work starts.

    Checked up front so a typo fails immediately rather than after a partial ingest.
    """
    if not series:
        return None
    unknown = [name for name in series if name not in set(all_series_ids())]
    if unknown:
        typer.echo(
            f"Unknown series: {', '.join(unknown)}. "
            f"Available: {', '.join(all_series_ids())}",
            err=True,
        )
        raise typer.Exit(2)
    return list(series)


def _parse_date(value: str) -> date:
    """Parse a YYYY-MM-DD option, exiting cleanly on a bad format."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        typer.echo(f"Invalid date '{value}'. Use YYYY-MM-DD.", err=True)
        raise typer.Exit(2) from None


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
