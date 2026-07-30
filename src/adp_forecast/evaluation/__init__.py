"""Evaluation layer: walk-forward backtesting and accuracy metrics."""

from .backtest import BacktestReport, Backtester, OriginOutcome, Scorecard
from .significance import (
    ALPHA,
    ComparisonResult,
    Loss,
    diebold_mariano,
)
from .metrics import (
    ScoreCard,
    directional_accuracy,
    interval_coverage,
    mean_absolute_error,
    mean_error,
    root_mean_squared_error,
    score,
)

__all__ = [
    "ALPHA",
    "BacktestReport",
    "ComparisonResult",
    "Loss",
    "diebold_mariano",
    "Backtester",
    "OriginOutcome",
    "ScoreCard",
    "Scorecard",
    "directional_accuracy",
    "interval_coverage",
    "mean_absolute_error",
    "mean_error",
    "root_mean_squared_error",
    "score",
]
