"""Explanation layer: turning a forecast into checkable plain English.

Every sentence is derived from the forecast's structured fields, and the claim that the
named drivers sum to the reported number is verified rather than assumed.
"""

from .narrative import (
    DEFAULT_DRIVER_COUNT,
    MATERIAL_CONTRIBUTION_K,
    DriverStatement,
    Explanation,
    ExplanationError,
    ForecastExplainer,
    explain_forecast,
)

__all__ = [
    "DEFAULT_DRIVER_COUNT",
    "MATERIAL_CONTRIBUTION_K",
    "DriverStatement",
    "Explanation",
    "ExplanationError",
    "ForecastExplainer",
    "explain_forecast",
]
