"""Unit tests for the single unit-conversion choke point.

The 1000x error this module prevents throws no exception and produces output that
looks plausible, so these tests carry more weight than their size suggests. The
architectural test at the bottom is the load-bearing one: it enforces that the
conversion happens in exactly one place, rather than trusting future readers to
remember.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from adp_forecast.config import TARGET_SERIES_ID
from adp_forecast.exceptions import ConfigurationError
from adp_forecast.units import (
    canonical_unit_label,
    is_count_series,
    observation_in_thousands,
    to_thousands,
)
from test_domain import make_observation  # reuse the shared builder

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

#: The exact figure FRED published for ADP private payrolls, June 2026.
ADP_JUNE_2026_RAW = 132_722_000.0
ADP_JUNE_2026_THOUSANDS = 132_722.0


# -- the conversion itself -----------------------------------------------------


def test_adp_persons_convert_to_thousands():
    """The pinned case: 132,722,000 Persons must read as 132,722 thousands."""
    assert to_thousands(TARGET_SERIES_ID, ADP_JUNE_2026_RAW) == pytest.approx(
        ADP_JUNE_2026_THOUSANDS
    )


def test_bls_thousands_pass_through_unchanged():
    """USPRIV already publishes thousands; converting again would divide twice."""
    assert to_thousands("USPRIV", 135_613.0) == pytest.approx(135_613.0)
    assert to_thousands("PAYEMS", 158_984.0) == pytest.approx(158_984.0)


def test_weekly_claims_convert_to_thousands():
    """ICSA publishes raw counts, so 187,000 claims is 187 thousand."""
    assert to_thousands("ICSA", 187_000.0) == pytest.approx(187.0)
    assert to_thousands("CCSA", 1_796_000.0) == pytest.approx(1_796.0)


def test_percentage_series_is_not_rescaled():
    """'Thousands of a percent' is meaningless; UNRATE must pass through."""
    assert to_thousands("UNRATE", 4.2) == pytest.approx(4.2)


def test_jolts_already_in_thousands():
    assert to_thousands("JTSJOL", 7_594.0) == pytest.approx(7_594.0)


def test_missing_values_stay_missing():
    """None must never become 0.0 — absence of data is not zero jobs."""
    assert to_thousands(TARGET_SERIES_ID, None) is None
    assert observation_in_thousands(make_observation(value=None)) is None


def test_zero_is_preserved_as_data():
    assert to_thousands(TARGET_SERIES_ID, 0.0) == pytest.approx(0.0)


def test_unknown_series_raises_rather_than_assuming_a_factor():
    with pytest.raises(ConfigurationError):
        to_thousands("NOT_A_SERIES", 1.0)


def test_double_application_is_detectably_wrong():
    """Documents the hazard the architectural test below exists to prevent."""
    once = to_thousands(TARGET_SERIES_ID, ADP_JUNE_2026_RAW)
    twice = to_thousands(TARGET_SERIES_ID, once)

    assert once == pytest.approx(132_722.0)
    assert twice == pytest.approx(132.722)
    assert twice != pytest.approx(once)


# -- observation-level helper --------------------------------------------------


def test_observation_helper_binds_series_id_to_value():
    """Passing an Observation makes it impossible to pair the wrong ID with a value."""
    obs = make_observation(value=135_613.0)  # USPRIV

    assert observation_in_thousands(obs) == pytest.approx(135_613.0)


# -- labelling -----------------------------------------------------------------


@pytest.mark.parametrize(
    "series_id, expected",
    [
        (TARGET_SERIES_ID, "thousands of persons"),
        ("ICSA", "thousands of persons"),
        ("USPRIV", "thousands of persons"),
        ("JTSJOL", "thousands of persons"),
        ("UNRATE", "percent"),
    ],
)
def test_canonical_unit_label(series_id, expected):
    assert canonical_unit_label(series_id) == expected


def test_is_count_series_separates_counts_from_rates():
    assert is_count_series(TARGET_SERIES_ID)
    assert is_count_series("ICSA")
    assert not is_count_series("UNRATE")


# -- architectural guarantee ---------------------------------------------------


def test_scale_factor_is_read_only_by_the_units_module():
    """Mechanically enforce one conversion point.

    ``scale_to_thousands`` may be *declared* in domain.py and config.py, and *read*
    only by units.py. Any other module touching it is a second conversion site, which
    reintroduces exactly the forget-it-or-do-it-twice failure this design removes.

    A test rather than a convention because conventions do not fail the build.
    """
    allowed = {"units.py", "domain.py", "config.py"}
    offenders = [
        path.relative_to(SRC_ROOT)
        for path in SRC_ROOT.rglob("*.py")
        if path.name not in allowed and "scale_to_thousands" in path.read_text()
    ]

    assert not offenders, (
        f"scale_to_thousands must only be read by units.py, but is referenced in: "
        f"{[str(path) for path in offenders]}. Route the conversion through "
        f"adp_forecast.units.to_thousands instead."
    )


def test_scripts_do_not_apply_their_own_scaling():
    """Entry points are readers too, and are the easiest place to forget."""
    scripts_root = Path(__file__).resolve().parents[1] / "scripts"
    offenders = [
        path.name
        for path in scripts_root.rglob("*.py")
        if "scale_to_thousands" in path.read_text()
    ]

    assert not offenders, f"scripts applying their own scale factor: {offenders}"


def test_no_magic_thousand_divisors_in_source():
    """Catch a hand-rolled ``/ 1000`` that bypasses the choke point entirely."""
    pattern = re.compile(r"/\s*1_?000(?![0-9_])")
    offenders = [
        path.relative_to(SRC_ROOT)
        for path in SRC_ROOT.rglob("*.py")
        if path.name != "units.py" and pattern.search(path.read_text())
    ]

    assert not offenders, (
        f"hand-rolled thousand divisor found in {[str(p) for p in offenders]}; "
        f"use adp_forecast.units.to_thousands"
    )
