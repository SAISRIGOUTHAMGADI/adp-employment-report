"""Design-matrix construction: a feature panel becomes ``X``, ``y`` and a prediction row.

Feature terms are **declared as data**, not built by procedural code, so the model's
inputs are inspectable, testable and reusable by the explanation layer — which reads the
same labels it renders.

Availability is enforced, not assumed
-------------------------------------
Each term carries a ``lag_months``, and :func:`build_design_matrix` refuses any term
whose lag is shorter than the series' registered ``publication_lag_months``. That makes
the classic backtest leak — using a figure that had not been published at forecast time
— a construction-time error rather than a silently optimistic score.

The lags are not arbitrary. Weekly claims for month *T* are fully published before ADP's
release for *T*, so lag 0 is genuine. BLS payrolls for *T* land two days **after** ADP,
so only *T-1* is available. JOLTS trails a further month, so *T-2*. The registry holds
those facts; this module is checked against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Final, Mapping, Sequence

import numpy as np

from ..config import TARGET_SERIES_ID, get_series_spec, is_excluded_month
from ..domain import MonthlyChange
from ..exceptions import ConfigurationError, InsufficientDataError
from ..features import FeaturePanel
from ..logging_config import get_logger

_LOG = get_logger(__name__)


class Transform(str, Enum):
    """What quantity a term draws from a series."""

    #: The month's value in canonical units (thousands, or percent for rates).
    LEVEL = "level"
    #: The month-over-month change in canonical units.
    CHANGE = "change"
    #: Mean of the month-over-month change over a trailing window.
    TRAILING_MEAN = "trailing_mean"


@dataclass(frozen=True, slots=True)
class FeatureTerm:
    """One column of the design matrix.

    Attributes:
        name: Machine-readable column name.
        series_id: Series the value is drawn from.
        transform: Level, month-over-month change, or trailing mean of changes.
        lag_months: Months back from the target month. Must be at least the series'
            registered publication lag.
        label: Human-readable description, surfaced verbatim in explanations.
        window: Months averaged, for :attr:`Transform.TRAILING_MEAN`. Ignored by the
            other transforms.
        min_periods: Fewest non-excluded months required inside ``window`` before a
            trailing mean is formed. Defaults to half the window, rounded up.
    """

    name: str
    series_id: str
    transform: Transform
    lag_months: int
    label: str
    window: int = 1
    min_periods: int | None = None

    @property
    def required_periods(self) -> int:
        """Effective minimum months for a trailing mean."""
        if self.min_periods is not None:
            return self.min_periods
        return (self.window + 1) // 2


#: The default model inputs.
#:
#: Deliberately small: 168 usable months after regime exclusion does not support a wide
#: matrix, and every term here has a stated reason to carry signal. Three own-lags
#: capture the target's momentum and mean reversion; claims supply the timeliest labour
#: signal in both level and change form; the remaining terms add the official and
#: demand-side picture at whatever lag they are genuinely available.
#:
#: The trailing mean leads the list because it fixes a diagnosed defect rather than
#: chasing a score. A ridge intercept is the training mean, and 72% of the usable
#: history predates 2020 and averages +180k against +54k since 2024 — leaving the model
#: anchored to a labour market that no longer exists, with a measured +18.7k bias. A
#: trailing mean of recent changes gives it a local anchor to track instead.
#:
#: Added on that structural argument alone. The window was *not* selected by comparing
#: backtest error across candidate lengths: choosing a hyperparameter by test-set
#: performance is the same leak the vintage design exists to prevent, and it would make
#: every number the evaluator reports meaningless.
DEFAULT_TERMS: Final[tuple[FeatureTerm, ...]] = (
    FeatureTerm(
        "adp_trailing_mean_12", TARGET_SERIES_ID, Transform.TRAILING_MEAN, 1,
        "ADP average change over the past year",
        window=12,
    ),
    FeatureTerm(
        "adp_change_lag1", TARGET_SERIES_ID, Transform.CHANGE, 1,
        "ADP change last month",
    ),
    FeatureTerm(
        "adp_change_lag2", TARGET_SERIES_ID, Transform.CHANGE, 2,
        "ADP change two months ago",
    ),
    FeatureTerm(
        "adp_change_lag3", TARGET_SERIES_ID, Transform.CHANGE, 3,
        "ADP change three months ago",
    ),
    FeatureTerm(
        "icsa_level", "ICSA", Transform.LEVEL, 0,
        "Initial claims level this month",
    ),
    FeatureTerm(
        "icsa_change", "ICSA", Transform.CHANGE, 0,
        "Initial claims change this month",
    ),
    FeatureTerm(
        "ccsa_change", "CCSA", Transform.CHANGE, 0,
        "Continued claims change this month",
    ),
    FeatureTerm(
        "usprv_change", "USPRIV", Transform.CHANGE, 1,
        "BLS private payroll change last month",
    ),
    FeatureTerm(
        "unrate_change", "UNRATE", Transform.CHANGE, 1,
        "Unemployment rate change last month",
    ),
    FeatureTerm(
        "jolts_change", "JTSJOL", Transform.CHANGE, 2,
        "Job openings change two months ago",
    ),
)


@dataclass(frozen=True, slots=True)
class DesignMatrix:
    """Training inputs plus the row to predict.

    Attributes:
        x: Training features, shape ``(n_samples, n_terms)``.
        y: Training targets — ADP month-over-month change in thousands, shape ``(n,)``.
        x_next: Feature row for the month being forecast, shape ``(n_terms,)``.
        months: Reference month of each training row, aligned with ``y``.
        terms: Column definitions, aligned with the columns of ``x``.
        target_month: The month ``x_next`` predicts.
        excluded_months: Rows dropped by regime exclusion.
        incomplete_months: Rows dropped for missing feature values.
    """

    x: np.ndarray
    y: np.ndarray
    x_next: np.ndarray
    months: tuple[date, ...]
    terms: tuple[FeatureTerm, ...]
    target_month: date
    excluded_months: tuple[date, ...]
    incomplete_months: tuple[date, ...]

    @property
    def n_samples(self) -> int:
        """Number of usable training rows."""
        return int(self.x.shape[0])

    @property
    def n_terms(self) -> int:
        """Number of feature columns."""
        return len(self.terms)


def build_design_matrix(
    panel: FeaturePanel,
    terms: Sequence[FeatureTerm] = DEFAULT_TERMS,
    *,
    min_samples: int = 24,
) -> DesignMatrix:
    """Assemble training data and the prediction row from a panel.

    Args:
        panel: Everything knowable at the forecast origin.
        terms: Column definitions. Defaults to :data:`DEFAULT_TERMS`.
        min_samples: Fewest training rows worth fitting on.

    Returns:
        A populated :class:`DesignMatrix`.

    Raises:
        ConfigurationError: If a term's lag is shorter than its series' registered
            publication lag, which would use unpublished data.
        InsufficientDataError: If fewer than ``min_samples`` complete rows survive, or
            if the prediction row itself has a missing value.
    """
    _validate_term_lags(terms)

    target_by_month = {change.month: change for change in panel.target_changes}
    feature_index = _index_panel(panel)

    rows: list[list[float]] = []
    kept_months: list[date] = []
    excluded: list[date] = []
    incomplete: list[date] = []

    for month in sorted(target_by_month):
        contributing = _contributing_months(month, terms)
        if any(is_excluded_month(value) for value in contributing):
            excluded.append(month)
            continue

        row = _build_row(month, terms, target_by_month, feature_index)
        if row is None:
            incomplete.append(month)
            continue

        rows.append(row)
        kept_months.append(month)

    if len(rows) < min_samples:
        raise InsufficientDataError(
            f"Only {len(rows)} complete training row(s) for {panel.target_month}; "
            f"{min_samples} required. Dropped {len(excluded)} to regime exclusion and "
            f"{len(incomplete)} for missing features."
        )

    next_row = _build_row(
        panel.target_month, terms, target_by_month, feature_index
    )
    if next_row is None:
        missing = _missing_terms(
            panel.target_month, terms, target_by_month, feature_index
        )
        raise InsufficientDataError(
            f"Cannot forecast {panel.target_month}: no value available for "
            f"{', '.join(missing)} as of {panel.as_of}."
        )

    _LOG.debug(
        "Design matrix for %s: %d rows x %d terms (excluded %d, incomplete %d)",
        panel.target_month,
        len(rows),
        len(terms),
        len(excluded),
        len(incomplete),
    )
    return DesignMatrix(
        x=np.asarray(rows, dtype=float),
        y=np.asarray(
            [target_by_month[month].change for month in kept_months], dtype=float
        ),
        x_next=np.asarray(next_row, dtype=float),
        months=tuple(kept_months),
        terms=tuple(terms),
        target_month=panel.target_month,
        excluded_months=tuple(excluded),
        incomplete_months=tuple(incomplete),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _validate_term_lags(terms: Sequence[FeatureTerm]) -> None:
    """Reject any term that would read data unpublished at forecast time.

    Raises:
        ConfigurationError: If a lag is negative, or shorter than the series'
            registered publication lag.
    """
    for term in terms:
        spec = get_series_spec(term.series_id)
        if term.lag_months < 0:
            raise ConfigurationError(
                f"Term '{term.name}' has a negative lag ({term.lag_months}), which "
                "would read the future."
            )
        if term.transform is Transform.TRAILING_MEAN and term.window < 2:
            raise ConfigurationError(
                f"Term '{term.name}' is a trailing mean with window={term.window}; "
                "use Transform.CHANGE for a single month."
            )
        if term.required_periods > term.window:
            raise ConfigurationError(
                f"Term '{term.name}' requires {term.required_periods} periods from a "
                f"{term.window}-month window, which can never be satisfied."
            )
        # The target's own value for month T is what we are predicting, so it needs a
        # lag of at least 1 regardless of the series' publication lag.
        minimum = max(spec.publication_lag_months, 1 if term.series_id == TARGET_SERIES_ID else 0)
        if term.lag_months < minimum:
            raise ConfigurationError(
                f"Term '{term.name}' uses {term.series_id} at lag {term.lag_months}, "
                f"but that series is only available at lag {minimum} when forecasting. "
                "Using it would leak unpublished data into the model."
            )


def _index_panel(panel: FeaturePanel) -> Mapping[tuple[str, Transform], Mapping[date, float]]:
    """Flatten a panel into ``(series, transform) -> {month: value}`` lookups.

    Built once per panel so row assembly is O(1) per cell rather than a scan, making
    matrix construction O(n_months x n_terms) overall.
    """
    index: dict[tuple[str, Transform], dict[date, float]] = {}

    for series_id, values in panel.feature_values.items():
        index[(series_id, Transform.LEVEL)] = {
            value.month: value.value for value in values if value.value is not None
        }
    for series_id, changes in panel.feature_changes.items():
        index[(series_id, Transform.CHANGE)] = {
            change.month: change.change for change in changes
        }
    return index


def _contributing_months(target_month: date, terms: Sequence[FeatureTerm]) -> list[date]:
    """Every reference month a row for ``target_month`` must have clean.

    Regime exclusion tests all of them: a row whose target sits outside the pandemic
    window but whose lagged claims come from inside it is still contaminated.

    Trailing-mean windows are deliberately *not* included. Requiring a full 12 clean
    months would drop every row for a year after the window closes — precisely the
    scarce post-regime data the term exists to exploit. Instead the mean itself skips
    excluded months (see :func:`_trailing_mean`), so it never averages contaminated
    values while the row survives.
    """
    months = [target_month]
    for term in terms:
        if term.transform is Transform.TRAILING_MEAN:
            continue
        months.append(_shift_months(target_month, -term.lag_months))
        if term.transform is Transform.CHANGE:
            # A change also depends on the month before it.
            months.append(_shift_months(target_month, -term.lag_months - 1))
    return months


def _trailing_mean(
    term: FeatureTerm,
    target_month: date,
    target_by_month: Mapping[date, MonthlyChange],
) -> float | None:
    """Mean target change over the term's trailing window, skipping excluded months.

    Averaging across the pandemic window would import +900k reopening months into an
    otherwise clean anchor, so those months are dropped from the average rather than
    the row being dropped from training. The result is ``None`` when fewer than
    ``term.required_periods`` clean months remain, so a thin anchor is reported absent
    rather than computed from one or two observations.

    O(window) per row.

    Args:
        term: The trailing-mean term.
        target_month: Month the row predicts.
        target_by_month: Target changes indexed by month.
    """
    values: list[float] = []
    for offset in range(term.window):
        month = _shift_months(target_month, -term.lag_months - offset)
        if is_excluded_month(month):
            continue
        change = target_by_month.get(month)
        if change is not None:
            values.append(change.change)

    if len(values) < term.required_periods:
        return None
    return sum(values) / len(values)


def _build_row(
    target_month: date,
    terms: Sequence[FeatureTerm],
    target_by_month: Mapping[date, MonthlyChange],
    feature_index: Mapping[tuple[str, Transform], Mapping[date, float]],
) -> list[float] | None:
    """Assemble one design row, or ``None`` if any term is unavailable."""
    row: list[float] = []
    for term in terms:
        value = _lookup(term, target_month, target_by_month, feature_index)
        if value is None:
            return None
        row.append(value)
    return row


def _missing_terms(
    target_month: date,
    terms: Sequence[FeatureTerm],
    target_by_month: Mapping[date, MonthlyChange],
    feature_index: Mapping[tuple[str, Transform], Mapping[date, float]],
) -> list[str]:
    """Names of terms with no value for ``target_month``, for error messages."""
    return [
        term.name
        for term in terms
        if _lookup(term, target_month, target_by_month, feature_index) is None
    ]


def _lookup(
    term: FeatureTerm,
    target_month: date,
    target_by_month: Mapping[date, MonthlyChange],
    feature_index: Mapping[tuple[str, Transform], Mapping[date, float]],
) -> float | None:
    """Resolve one term's value for one target month, or ``None`` if absent."""
    source_month = _shift_months(target_month, -term.lag_months)

    if term.series_id == TARGET_SERIES_ID:
        if term.transform is Transform.TRAILING_MEAN:
            return _trailing_mean(term, target_month, target_by_month)
        if term.transform is not Transform.CHANGE:
            # The target is modelled as a change; its level is a rebenchmark-dependent
            # quantity that would be meaningless as a regressor.
            raise ConfigurationError(
                f"Term '{term.name}': the target series is only usable as a change."
            )
        change = target_by_month.get(source_month)
        return None if change is None else change.change

    if term.transform is Transform.TRAILING_MEAN:
        raise ConfigurationError(
            f"Term '{term.name}': trailing means are only implemented for the target."
        )
    return feature_index.get((term.series_id, term.transform), {}).get(source_month)


def _shift_months(value: date, offset: int) -> date:
    """Return the first of the month ``offset`` months from ``value``'s month."""
    total = value.year * 12 + (value.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)
