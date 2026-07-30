"""Presentation helpers for the CLI.

Kept apart from :mod:`adp_forecast.cli.app` so that command wiring and output formatting
can change independently, and so a future HTTP layer can reuse the service calls without
inheriting any of this.

Nothing here computes anything. Every function takes a typed object produced by a lower
layer and turns it into text — which is the whole point of having the service layer
return dataclasses rather than pre-formatted strings.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Sequence

from ..domain import Observation
from ..evaluation import BacktestReport, Loss, Scorecard
from ..explanation import Explanation
from ..forecast import Forecast
from ..units import canonical_unit_label, observation_in_thousands

_RULE_WIDTH = 78


def render_history(
    observations: Sequence[Observation],
    series_id: str,
    label: str,
    count: int,
) -> str:
    """Render recent observations as a table with month-over-month changes.

    Args:
        observations: Full series, ascending. The change for the first displayed row is
            computed from its true predecessor rather than shown as a gap.
        series_id: Series being displayed, used for unit conversion.
        label: Human-readable series name.
        count: How many recent rows to show.
    """
    window = observations[-count:]
    unit = canonical_unit_label(series_id)
    lines = [
        f"{label} — last {len(window)} observations ({unit})",
        f"{'reference':<12}{'level':>16}{'MoM change':>14}",
        "-" * 42,
    ]

    start = len(observations) - len(window)
    for offset, observation in enumerate(window):
        level = observation_in_thousands(observation)
        if level is None:
            lines.append(f"{observation.date.isoformat():<12}{'(missing)':>16}{'':>14}")
            continue

        index = start + offset
        previous = observations[index - 1] if index else None
        previous_level = (
            observation_in_thousands(previous) if previous is not None else None
        )
        change = f"{level - previous_level:+,.0f}k" if previous_level is not None else "—"
        lines.append(f"{observation.date.isoformat():<12}{level:>16,.0f}{change:>14}")
    return "\n".join(lines)


def render_explanation(explanation: Explanation) -> str:
    """Render a forecast explanation as plain text."""
    return explanation.to_text(width=_RULE_WIDTH)


def render_backtest(report: BacktestReport) -> str:
    """Render one backtest scorecard as a table."""
    headers = {
        Scorecard.VINTAGE: (
            "VINTAGE-CORRECT SCORECARD (headline)\n"
            "  Point-in-time panels; scored against the print ADP actually published."
        ),
        Scorecard.LAG_SHIFTED: (
            "LAG-SHIFTED SCORECARD (approximate — wider coverage, weaker guarantee)\n"
            "  Current-vintage data truncated by declared publication lags. Uses\n"
            "  revised figures where a forecaster had first prints, so it cannot\n"
            "  measure revision effects and may flatter every model."
        ),
    }

    lines = [
        "=" * _RULE_WIDTH,
        headers[report.scorecard],
        "=" * _RULE_WIDTH,
        f"origins attempted: {report.n_attempted}   "
        f"scored by all models: {report.n_scored}   dropped: {report.n_dropped}",
    ]
    if report.n_scored:
        lines.append(
            f"period: {min(report.common_origins)} .. {max(report.common_origins)}"
        )

    lines.extend(
        [
            "",
            f"{'model':14}{'n':>5}{'MAE':>9}{'RMSE':>9}{'bias':>9}"
            f"{'dir%':>8}{'cover':>8}{'gap':>8}{'width':>9}",
            "-" * _RULE_WIDTH,
        ]
    )
    for name in report.models:
        card = report.scores[name]
        direction = (
            f"{100 * card.directional_accuracy:.0f}%"
            if card.directional_accuracy is not None
            else "-"
        )
        if card.interval_coverage is None:
            coverage = gap = width = "-"
        else:
            coverage = f"{100 * card.interval_coverage:.0f}%"
            gap = f"{100 * card.coverage_gap(report.interval_level):+.0f}pp"
            width = f"{card.mean_interval_width:.0f}k"
        lines.append(
            f"{name:14}{card.n:>5}{card.mae:>9.1f}{card.rmse:>9.1f}{card.bias:>+9.1f}"
            f"{direction:>8}{coverage:>8}{gap:>8}{width:>9}"
        )

    lines.append(f"\nbest MAE: {report.best_by_mae()}")
    lines.extend(_render_significance(report))
    lines.extend(_render_skips(report))
    return "\n".join(lines)


def _render_significance(report: BacktestReport) -> list[str]:
    """Report whether the best model's margin survives a paired significance test.

    Printed alongside the table because a ranking without it invites the reader to treat
    a 2% MAE gap on 39 observations as a result.
    """
    best = report.best_by_mae()
    rivals = [name for name in report.models if name != best]
    if not rivals:
        return []

    lines = [
        "",
        f"Is {best}'s margin real? Diebold-Mariano, paired on the same origins:",
        f"  {'vs':14}{'loss':10}{'diff':>9}{'t':>8}{'p':>8}  verdict",
        "  " + "-" * 74,
    ]
    for rival in rivals:
        for loss in (Loss.ABSOLUTE, Loss.SQUARED):
            try:
                result = report.compare(best, rival, loss=loss)
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                lines.append(f"  {rival:14}{loss.value:10}  not testable: {exc}")
                continue
            marker = "significant" if result.is_significant else "indistinguishable"
            lines.append(
                f"  {rival:14}{loss.value:10}{result.mean_differential:>+9.1f}"
                f"{result.statistic:>8.2f}{result.p_value:>8.3f}  {marker}"
            )
    lines.append(
        "  (negative diff favours " + best + "; p < 0.05 would mean the gap is real)"
    )
    return lines


def _render_skips(report: BacktestReport) -> list[str]:
    """Report which models failed where, so dropped origins are never silent."""
    reasons: dict[str, int] = {}
    for outcome in report.outcomes:
        for name in outcome.skipped:
            reasons[name] = reasons.get(name, 0) + 1
    if not reasons:
        return []
    lines = ["", "origins a model could not forecast:"]
    lines.extend(f"  {name:14} {count}" for name, count in sorted(reasons.items()))
    return lines


def render_ingest(report: Any, database: Any, total_stored: int) -> str:
    """Render an ingest run summary."""
    lines = [
        f"Ingest complete in {report.duration_seconds:.1f}s -> {database}",
        f"{'series':16}{'rows':>9}{'through':>13}  status",
        "-" * 52,
    ]
    for result in report.results:
        through = result.max_obs_date.isoformat() if result.max_obs_date else "-"
        status = "ok" if result.succeeded else f"FAILED ({type(result.error).__name__})"
        lines.append(
            f"{result.series_id:16}{result.rows_written:>9}{through:>13}  {status}"
        )
    lines.extend(
        [
            "-" * 52,
            f"{'TOTAL':16}{report.rows_written:>9}",
            "",
            f"Release dates stored: {report.release_dates_written}",
            f"Observations in store: {total_stored:,}",
        ]
    )
    if not report.succeeded:
        lines.append(f"\n{len(report.failures)} series failed; see log output above.")
    return "\n".join(lines)


def forecast_as_json(forecast: Forecast, explanation: Explanation) -> str:
    """Serialise a forecast and its reasoning as JSON.

    The payload a web UI or HTTP endpoint would return. It exists because the service
    layer returns dataclasses rather than strings — without that, this would mean
    re-parsing prose.

    Args:
        forecast: The forecast.
        explanation: Its explanation.
    """
    payload = {
        "series_id": forecast.series_id,
        "month": forecast.month.isoformat(),
        "as_of": forecast.as_of.isoformat(),
        "model": forecast.model_name,
        "point_thousands": round(forecast.point, 2),
        "lower_thousands": _round_or_none(forecast.lower),
        "upper_thousands": _round_or_none(forecast.upper),
        "interval_level": forecast.interval_level,
        "n_train": forecast.n_train,
        "baseline_point_thousands": _round_or_none(forecast.baseline_point),
        "headline": explanation.headline,
        "interval_statement": explanation.interval,
        "context": explanation.context,
        "anchor": explanation.anchor,
        "drivers": [asdict(statement) for statement in explanation.drivers],
        "caveats": list(explanation.caveats),
    }
    return json.dumps(payload, indent=2, default=_encode)


def _round_or_none(value: float | None) -> float | None:
    """Round a value to two decimals, preserving ``None``."""
    return None if value is None else round(value, 2)


def _encode(value: object) -> str:
    """JSON encoder for dates and datetimes."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialise {type(value).__name__}")
