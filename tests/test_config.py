"""Unit tests for configuration loading and the series registry."""

from __future__ import annotations

import pytest

from adp_forecast.config import (
    SERIES_REGISTRY,
    TARGET_SERIES_ID,
    all_series_ids,
    get_api_key,
    get_series_spec,
    require_api_key,
    series_ids_for_role,
)
from adp_forecast.domain import Frequency, SeriesRole
from adp_forecast.exceptions import ConfigurationError

VALID_KEY = "b" * 32


def test_require_api_key_returns_configured_key(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", VALID_KEY)
    assert require_api_key() == VALID_KEY


def test_require_api_key_raises_when_absent(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="not set"):
        require_api_key()


def test_require_api_key_raises_on_wrong_length(monkeypatch):
    """Catching the shape locally turns an opaque HTTP 400 into a clear message."""
    monkeypatch.setenv("FRED_API_KEY", "tooshort")
    with pytest.raises(ConfigurationError, match="32 alphanumeric"):
        require_api_key()


def test_require_api_key_rejects_non_alphanumeric(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "!" * 32)
    with pytest.raises(ConfigurationError):
        require_api_key()


def test_whitespace_only_key_is_treated_as_absent(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "   ")
    assert get_api_key() is None


def test_registry_contains_exactly_one_target():
    targets = series_ids_for_role(SeriesRole.TARGET)
    assert targets == (TARGET_SERIES_ID,)


def test_registry_covers_the_agreed_series_set():
    expected = {
        TARGET_SERIES_ID,
        "ICSA",
        "CCSA",
        "USPRIV",
        "PAYEMS",
        "UNRATE",
        "JTSJOL",
    }
    assert set(all_series_ids()) == expected


def test_target_is_listed_first():
    """Downstream output leads with the target; registry order carries that."""
    assert all_series_ids()[0] == TARGET_SERIES_ID


def test_unknown_series_raises_with_helpful_message():
    with pytest.raises(ConfigurationError, match="Unknown series"):
        get_series_spec("NPPTTL")


def test_adp_units_scale_to_thousands():
    """ADP publishes Persons; the registry must normalise it to thousands."""
    spec = get_series_spec(TARGET_SERIES_ID)
    assert spec.units == "Persons"
    assert spec.scale_to_thousands == pytest.approx(0.001)
    assert 132_722_000 * spec.scale_to_thousands == pytest.approx(132_722)


def test_bls_series_need_no_rescaling():
    for series_id in ("USPRIV", "PAYEMS"):
        assert get_series_spec(series_id).scale_to_thousands == pytest.approx(1.0)


def test_jolts_carries_the_extra_publication_lag():
    """JOLTS trails the other monthly series by a month; features must respect it."""
    assert get_series_spec("JTSJOL").publication_lag_months == 2
    assert get_series_spec("USPRIV").publication_lag_months == 1


def test_weekly_series_have_no_publication_lag():
    for series_id in ("ICSA", "CCSA"):
        spec = get_series_spec(series_id)
        assert spec.frequency is Frequency.WEEKLY
        assert spec.publication_lag_months == 0


def test_every_series_documents_why_it_is_included():
    """The explanation layer sources its prose here, so blanks are a defect."""
    for spec in SERIES_REGISTRY.values():
        assert spec.description.strip(), f"{spec.series_id} has no description"
        assert spec.label.strip()


def test_registry_is_immutable():
    with pytest.raises(TypeError):
        SERIES_REGISTRY["FOO"] = None  # type: ignore[index]
