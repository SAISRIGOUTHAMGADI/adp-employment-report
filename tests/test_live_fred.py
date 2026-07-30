"""Optional integration tests against the real FRED API.

Skipped automatically when ``FRED_API_KEY`` is absent, so ``pytest`` stays green on
a fresh clone with no credentials. Deselect explicitly with::

    pytest -m "not live"

These assert on *invariants* rather than on specific values. Asserting that June
2026 equals 132,722,000 would turn next month's routine data update into a red
build. What must hold is that the series exists, is monthly, is plausibly scaled,
and that the vintage machinery behaves as the design assumes.
"""

from __future__ import annotations

import os
from datetime import date

import pytest

from adp_forecast.config import (
    ADP_RELEASE_ID,
    TARGET_SERIES_ID,
    FredSettings,
    get_api_key,
    get_series_spec,
)
from adp_forecast.exceptions import AuthenticationError, SeriesNotFoundError
from adp_forecast.ingestion import FredAdapter

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        get_api_key() is None,
        reason="FRED_API_KEY not set; skipping live API tests",
    ),
]

#: Opt-out for sandboxed CI where the host is reachable but egress is filtered.
_NETWORK_DISABLED = os.getenv("ADP_DISABLE_NETWORK_TESTS") == "1"
pytestmark.append(
    pytest.mark.skipif(_NETWORK_DISABLED, reason="ADP_DISABLE_NETWORK_TESTS=1")
)


@pytest.fixture(scope="module")
def adapter():
    """One adapter for the module, so the session is reused across tests."""
    with FredAdapter(FredSettings.from_env()) as instance:
        yield instance


def test_target_series_is_monthly_and_plausibly_scaled(adapter):
    observations = adapter.fetch(TARGET_SERIES_ID, start=date(2024, 1, 1))
    values = [obs for obs in observations if obs.value is not None]

    assert len(values) >= 12, "expected at least a year of monthly observations"
    assert all(obs.date.day == 1 for obs in values), "monthly series must be month-start"

    spec = get_series_spec(TARGET_SERIES_ID)
    latest_thousands = values[-1].value * spec.scale_to_thousands
    # US private payrolls sit near 130 million, i.e. ~130,000 thousands. A band this
    # wide still catches the 1000x units error the scaling exists to prevent.
    assert 100_000 < latest_thousands < 200_000


def test_observations_are_returned_in_ascending_date_order(adapter):
    observations = adapter.fetch(TARGET_SERIES_ID, start=date(2024, 1, 1))
    dates = [obs.date for obs in observations]

    assert dates == sorted(dates)


def test_monthly_changes_are_within_a_plausible_range(adapter):
    """Guards the units contract end to end: MoM change must read as ~100k, not ~100M."""
    spec = get_series_spec(TARGET_SERIES_ID)
    observations = [
        obs
        for obs in adapter.fetch(TARGET_SERIES_ID, start=date(2023, 1, 1))
        if obs.value is not None
    ]

    changes = [
        (b.value - a.value) * spec.scale_to_thousands
        for a, b in zip(observations, observations[1:])
    ]

    assert changes, "need at least two observations to compute a change"
    # Excludes the January rebenchmark, which legitimately shifts the level by
    # millions and is masked out of the modelling window for that reason.
    non_rebenchmark = [
        change
        for change, obs in zip(changes, observations[1:])
        if obs.date.month != 1
    ]
    assert all(abs(change) < 1_500 for change in non_rebenchmark), (
        f"implausible monthly change in thousands: {non_rebenchmark}"
    )


def test_all_vintages_returns_more_rows_than_current_vintage(adapter):
    """The premise of the whole storage design: revisions exist and are retrievable."""
    current = adapter.fetch("USPRIV", start=date(2020, 1, 1))
    vintages = adapter.fetch("USPRIV", start=date(2020, 1, 1), all_vintages=True)

    assert len(vintages) > len(current)
    assert any(not obs.is_current_vintage for obs in vintages)


def test_vintage_windows_do_not_overlap_for_one_reference_period(adapter):
    """Overlapping windows would make `known_on` ambiguous and the backtest unsound."""
    vintages = [
        obs
        for obs in adapter.fetch("USPRIV", start=date(2024, 1, 1), all_vintages=True)
        if obs.date == date(2024, 6, 1)
    ]

    assert len(vintages) >= 2, "USPRIV is revised; expected multiple vintages"
    ordered = sorted(vintages, key=lambda obs: obs.realtime_start)
    for earlier, later in zip(ordered, ordered[1:]):
        assert earlier.realtime_end < later.realtime_start


def test_release_dates_are_real_and_ascending(adapter):
    dates = adapter.fetch_release_dates(ADP_RELEASE_ID, start=date(2024, 1, 1))

    assert len(dates) >= 12, "ADP publishes monthly"
    assert dates == sorted(dates)
    assert len(set(dates)) == len(dates), "release dates must be unique"


def test_bad_series_id_raises_series_not_found(adapter):
    with pytest.raises(SeriesNotFoundError):
        adapter.fetch("DEFINITELY_NOT_A_SERIES")


def test_bad_api_key_raises_authentication_error():
    """Confirms the live error body still matches what the classifier keys on."""
    settings = FredSettings(api_key="0" * 32, max_retries=0)
    with FredAdapter(settings) as adapter, pytest.raises(AuthenticationError):
        adapter.fetch(TARGET_SERIES_ID)
