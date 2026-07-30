"""The single place raw upstream values are converted to canonical model units.

Why this module exists at all: ADP publishes ``Persons`` (132,722,000) while BLS
publishes ``Thousands of Persons`` (135,613). Mixing the two is a 1000x error that
throws no exception and looks superficially plausible in output. Letting each reader
apply its own conversion invites two failure modes — forgetting it, and applying it
twice — neither of which surfaces as an error.

So the conversion lives here and only here. ``SeriesSpec.scale_to_thousands`` is
declared in the registry and read *only* by this module; a test in
``tests/test_units.py`` asserts that mechanically across the whole source tree, so the
guarantee is enforced rather than merely documented.

Readers call :func:`to_thousands` (raw float) or :func:`observation_in_thousands`
(an :class:`~adp_forecast.domain.Observation`) and never see a scale factor.
"""

from __future__ import annotations

from .config import get_series_spec
from .domain import Observation

#: Units that count people, and therefore carry a real magnitude conversion. Series
#: in other units (percentages, ratios, indices) pass through unchanged — see
#: :func:`to_thousands`.
_COUNT_UNITS: frozenset[str] = frozenset(
    {"Persons", "Number", "Thousands of Persons", "Level in Thousands"}
)


def to_thousands(series_id: str, value: float | None) -> float | None:
    """Convert one raw upstream value to canonical units.

    For series that count people, canonical units are *thousands of persons* — the
    scale every published payroll figure is quoted in. For series measured in
    anything else (``UNRATE`` is a percentage) the factor is 1.0 and the value passes
    through untouched, because "thousands of a percent" is meaningless.

    Args:
        series_id: Registered series identifier. The scale factor is looked up from
            the registry rather than accepted from the caller, so a caller cannot
            supply the wrong one.
        value: Raw value as published upstream, or ``None`` for a missing
            observation.

    Returns:
        The value in canonical units, or ``None`` if ``value`` was ``None``. Missing
        data stays missing; it is never silently coerced to 0.0.

    Raises:
        ConfigurationError: If ``series_id`` is not registered.

    Example:
        >>> to_thousands("ADPMNUSNERSA", 132_722_000.0)
        132722.0
        >>> to_thousands("USPRIV", 135_613.0)
        135613.0
    """
    spec = get_series_spec(series_id)
    if value is None:
        return None
    return value * spec.scale_to_thousands


def observation_in_thousands(observation: Observation) -> float | None:
    """Convert an :class:`~adp_forecast.domain.Observation` to canonical units.

    Preferred over calling :func:`to_thousands` with unpacked fields: it keeps the
    series ID and the value bound together, so they cannot be mismatched.

    Args:
        observation: The record to convert.

    Returns:
        The observation's value in canonical units, or ``None`` if missing.
    """
    return to_thousands(observation.series_id, observation.value)


def canonical_unit_label(series_id: str) -> str:
    """Return the display label for a series' canonical units.

    Used by output formatting so headers describe what the numbers actually are
    after conversion, rather than repeating the raw upstream unit string.

    Args:
        series_id: Registered series identifier.
    """
    spec = get_series_spec(series_id)
    if spec.units in _COUNT_UNITS:
        return "thousands of persons"
    return spec.units.lower()


def is_count_series(series_id: str) -> bool:
    """Whether a series counts people, and so is comparable to a payroll figure.

    Args:
        series_id: Registered series identifier.
    """
    return get_series_spec(series_id).units in _COUNT_UNITS
