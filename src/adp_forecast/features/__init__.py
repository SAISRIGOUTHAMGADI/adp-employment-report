"""Feature layer: frequency normalisation, vintage-safe differencing, panel assembly.

Two invariants hold across everything here:

* **One snapshot per panel.** Every value in a :class:`FeaturePanel` was the published
  truth on the same ``as_of`` date.
* **No arithmetic across vintages.** Differencing refuses operands from incompatible
  vintages and raises rather than producing a number that was never published.
"""

from .aggregation import (
    DEFAULT_MIN_WEEKS,
    AggregationMethod,
    aggregate_to_monthly,
    monthly_values_from_monthly_observations,
)
from .builder import RELEASE_ORIGIN_OFFSET, FeaturePanel, FeaturePanelBuilder
from .changes import change_series, month_over_month_change, monthly_value_changes

__all__ = [
    "DEFAULT_MIN_WEEKS",
    "RELEASE_ORIGIN_OFFSET",
    "AggregationMethod",
    "FeaturePanel",
    "FeaturePanelBuilder",
    "aggregate_to_monthly",
    "change_series",
    "month_over_month_change",
    "monthly_value_changes",
    "monthly_values_from_monthly_observations",
]
