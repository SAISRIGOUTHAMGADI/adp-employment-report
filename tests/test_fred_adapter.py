"""Unit tests for :class:`adp_forecast.ingestion.fred.FredAdapter`.

Entirely offline: every test injects a :class:`FakeSession`. The error-shape
fixtures are verbatim copies of real FRED responses captured on 2026-07-30, so the
classification logic is tested against what upstream actually sends rather than
against an invented payload.
"""

from __future__ import annotations

from datetime import date

import pytest

from adp_forecast.domain import CURRENT_VINTAGE_SENTINEL, EARLIEST_REALTIME
from adp_forecast.exceptions import (
    AuthenticationError,
    PermanentIngestionError,
    RateLimitError,
    ResponseValidationError,
    SeriesNotFoundError,
    TransientIngestionError,
)
from adp_forecast.ingestion import IngestionPort, ReleaseCalendarPort
from adp_forecast.ingestion.fred import FredAdapter
from conftest import FakeResponse, FakeSession, load_fixture

TARGET = "ADPMNUSNERSA"


def make_adapter(settings, responses) -> tuple[FredAdapter, FakeSession]:
    """Build an adapter wired to a scripted session."""
    session = FakeSession(responses)
    return FredAdapter(settings, session=session), session


# -- contract ------------------------------------------------------------------


def test_adapter_satisfies_both_ports(settings):
    """The adapter must structurally conform to the declared protocols."""
    adapter, _ = make_adapter(settings, [])
    assert isinstance(adapter, IngestionPort)
    assert isinstance(adapter, ReleaseCalendarPort)
    assert adapter.source_name == "FRED"


# -- happy path ----------------------------------------------------------------


def test_fetch_parses_observations(settings):
    payload = load_fixture("fred_observations_current")
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    observations = adapter.fetch(TARGET)

    assert len(observations) == 6
    first = observations[0]
    assert first.series_id == TARGET
    assert first.date == date(2026, 1, 1)
    assert first.value == pytest.approx(132_270_000.0)
    assert first.source == "FRED"
    assert first.fetched_at.tzinfo is not None, "fetched_at must be timezone-aware"


def test_missing_value_token_becomes_none(settings):
    """FRED's '.' must coerce to None, not to 0.0 and not raise."""
    payload = load_fixture("fred_observations_current")
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    observations = adapter.fetch(TARGET)
    march = next(obs for obs in observations if obs.date == date(2026, 3, 1))

    assert march.value is None
    assert march.is_missing


def test_all_observations_share_one_fetched_at(settings):
    """One call is one provenance event, so the timestamp must not vary per row."""
    payload = load_fixture("fred_observations_current")
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    observations = adapter.fetch(TARGET)

    assert len({obs.fetched_at for obs in observations}) == 1


# -- request construction ------------------------------------------------------


def test_start_date_becomes_observation_start(settings):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    adapter.fetch(TARGET, start=date(2009, 1, 1))

    _url, params = session.calls[0]
    assert params["observation_start"] == "2009-01-01"
    assert params["series_id"] == TARGET
    assert params["file_type"] == "json"


def test_current_vintage_request_omits_realtime_params(settings):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    adapter.fetch(TARGET)

    _url, params = session.calls[0]
    assert "realtime_start" not in params
    assert "realtime_end" not in params


def test_all_vintages_requests_widest_realtime_window(settings):
    """The full revision history depends on this exact parameter pair."""
    payload = load_fixture("fred_observations_vintages")
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    adapter.fetch("USPRIV", all_vintages=True)

    _url, params = session.calls[0]
    assert params["realtime_start"] == EARLIEST_REALTIME.isoformat()
    assert params["realtime_end"] == CURRENT_VINTAGE_SENTINEL.isoformat()


def test_api_key_is_sent_but_never_logged(settings, caplog):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    with caplog.at_level("DEBUG"):
        adapter.fetch(TARGET)

    _url, params = session.calls[0]
    assert params["api_key"] == settings.api_key
    assert settings.api_key not in caplog.text


# -- vintage semantics ---------------------------------------------------------


def test_current_vintage_end_is_normalised_to_sentinel(settings):
    """FRED reports today..today for a current query; storing that would break known_on."""
    payload = load_fixture("fred_observations_current")
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    observations = adapter.fetch(TARGET)

    assert all(obs.realtime_end == CURRENT_VINTAGE_SENTINEL for obs in observations)
    assert all(obs.is_current_vintage for obs in observations)


def test_all_vintages_preserves_real_windows(settings):
    payload = load_fixture("fred_observations_vintages")
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    observations = adapter.fetch("USPRIV", all_vintages=True)
    april = [obs for obs in observations if obs.date == date(2026, 4, 1)]

    assert len(april) == 3, "April 2026 was revised twice"
    assert april[0].realtime_start == date(2026, 5, 8)
    assert april[0].realtime_end == date(2026, 6, 4)
    assert april[0].value == pytest.approx(135_428)
    assert april[-1].is_current_vintage


def test_known_on_reconstructs_point_in_time_value(settings):
    """The backtest's core guarantee: as-of filtering returns the first print."""
    payload = load_fixture("fred_observations_vintages")
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    observations = adapter.fetch("USPRIV", all_vintages=True)
    as_of = date(2026, 5, 20)
    visible = [obs for obs in observations if obs.known_on(as_of)]

    assert len(visible) == 1, "only April's first print existed on 2026-05-20"
    assert visible[0].date == date(2026, 4, 1)
    assert visible[0].value == pytest.approx(135_428)


# -- error classification ------------------------------------------------------


def test_bad_series_raises_series_not_found(settings):
    payload = load_fixture("fred_error_bad_series")
    adapter, _ = make_adapter(settings, [FakeResponse(status_code=400, payload=payload)])

    with pytest.raises(SeriesNotFoundError):
        adapter.fetch("NOPE_XYZ")


def test_bad_key_raises_authentication_error(settings):
    payload = load_fixture("fred_error_bad_key")
    adapter, _ = make_adapter(settings, [FakeResponse(status_code=400, payload=payload)])

    with pytest.raises(AuthenticationError):
        adapter.fetch(TARGET)


def test_permanent_errors_are_not_retried(settings, no_sleep):
    """A 400 must cost exactly one request, not max_retries + 1."""
    payload = load_fixture("fred_error_bad_series")
    adapter, session = make_adapter(
        settings,
        [FakeResponse(status_code=400, payload=payload)] * 5,
    )

    with pytest.raises(SeriesNotFoundError):
        adapter.fetch("NOPE_XYZ")

    assert len(session.calls) == 1


def test_rate_limit_is_transient(settings, no_sleep):
    adapter, _ = make_adapter(
        settings,
        [FakeResponse(status_code=429, payload={"error_message": "too many"})] * 3,
    )

    with pytest.raises(RateLimitError):
        adapter.fetch(TARGET)


def test_non_json_body_raises_validation_error(settings, no_sleep):
    """Akamai-style HTML interception must not be mistaken for data."""
    adapter, _ = make_adapter(
        settings,
        [FakeResponse(status_code=200, payload=None, text="<HTML>Access Denied</HTML>")],
    )

    with pytest.raises(ResponseValidationError):
        adapter.fetch(TARGET)


def test_unparseable_value_raises_rather_than_nulling(settings):
    """A units or format change must fail loudly, not masquerade as missing data."""
    payload = load_fixture("fred_observations_current")
    payload["observations"][0]["value"] = "132,270,000"
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    with pytest.raises(ResponseValidationError):
        adapter.fetch(TARGET)


def test_missing_observations_key_raises_validation_error(settings):
    adapter, _ = make_adapter(settings, [FakeResponse(payload={"count": 0})])

    with pytest.raises(ResponseValidationError):
        adapter.fetch(TARGET)


def test_malformed_date_raises_validation_error(settings):
    payload = load_fixture("fred_observations_current")
    payload["observations"][0]["date"] = "not-a-date"
    adapter, _ = make_adapter(settings, [FakeResponse(payload=payload)])

    with pytest.raises(ResponseValidationError):
        adapter.fetch(TARGET)


# -- transport resilience ------------------------------------------------------


def test_timeout_is_retried_then_succeeds(settings, no_sleep, timeout_error):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(
        settings,
        [timeout_error, FakeResponse(payload=payload)],
    )

    observations = adapter.fetch(TARGET)

    assert len(observations) == 6
    assert len(session.calls) == 2, "one failure plus one success"


def test_connection_error_is_retried(settings, no_sleep, connection_error):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(
        settings,
        [connection_error, connection_error, FakeResponse(payload=payload)],
    )

    assert len(adapter.fetch(TARGET)) == 6
    assert len(session.calls) == 3


def test_retries_are_exhausted_then_raise(settings, no_sleep, timeout_error):
    """max_retries=2 means three attempts total, then surface the failure."""
    adapter, session = make_adapter(settings, [timeout_error] * 3)

    with pytest.raises(TransientIngestionError):
        adapter.fetch(TARGET)

    assert len(session.calls) == 3


def test_server_error_is_transient(settings, no_sleep):
    adapter, _ = make_adapter(
        settings,
        [FakeResponse(status_code=503, payload={"error_message": "unavailable"})] * 3,
    )

    with pytest.raises(TransientIngestionError):
        adapter.fetch(TARGET)


def test_unexpected_request_exception_is_permanent(settings, no_sleep):
    import requests

    adapter, session = make_adapter(settings, [requests.URLRequired("bad url")] * 3)

    with pytest.raises(PermanentIngestionError):
        adapter.fetch(TARGET)

    assert len(session.calls) == 1, "client-side defects must not be retried"


# -- pagination ----------------------------------------------------------------


def test_pagination_follows_offset_until_count_reached(settings):
    """A count larger than one page must trigger a second request."""
    page_one = load_fixture("fred_observations_current")
    page_one["count"] = 12
    page_two = load_fixture("fred_observations_current")
    page_two["count"] = 12

    adapter, session = make_adapter(
        settings,
        [FakeResponse(payload=page_one), FakeResponse(payload=page_two)],
    )

    observations = adapter.fetch(TARGET)

    assert len(observations) == 12
    assert len(session.calls) == 2
    assert session.calls[0][1]["offset"] == 0
    assert session.calls[1][1]["offset"] == 6


def test_single_page_issues_one_request(settings):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    adapter.fetch(TARGET)

    assert len(session.calls) == 1


def test_observations_use_the_100k_page_limit(settings):
    payload = load_fixture("fred_observations_current")
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    adapter.fetch(TARGET)

    assert session.calls[0][1]["limit"] == 100_000


def test_release_dates_use_the_10k_page_limit(settings):
    """FRED rejects limit > 10000 on this endpoint with HTTP 400, it does not clamp."""
    payload = {"count": 1, "release_dates": [{"release_id": 194, "date": "2026-07-01"}]}
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    adapter.fetch_release_dates(194)

    assert session.calls[0][1]["limit"] == 10_000


# -- release calendar ----------------------------------------------------------


def test_fetch_release_dates_parses_and_sorts(settings):
    payload = {
        "count": 3,
        "release_dates": [
            {"release_id": 194, "date": "2026-05-06"},
            {"release_id": 194, "date": "2026-06-03"},
            {"release_id": 194, "date": "2026-07-01"},
        ],
    }
    adapter, session = make_adapter(settings, [FakeResponse(payload=payload)])

    dates = adapter.fetch_release_dates(194)

    assert dates == [date(2026, 5, 6), date(2026, 6, 3), date(2026, 7, 1)]
    _url, params = session.calls[0]
    assert params["include_release_dates_with_no_data"] == "true"


def test_unknown_release_warns_because_fred_returns_empty_not_an_error(settings, caplog):
    """An unknown release_id yields HTTP 200 + [], so silence would hide a typo."""
    adapter, _ = make_adapter(
        settings, [FakeResponse(payload={"count": 0, "release_dates": []})]
    )

    with caplog.at_level("WARNING"):
        assert adapter.fetch_release_dates(99999999) == []

    assert "verify the ID" in caplog.text


# -- lifecycle -----------------------------------------------------------------


def test_context_manager_does_not_close_injected_session(settings):
    """The adapter must not close a session it does not own."""
    session = FakeSession([])
    with FredAdapter(settings, session=session):
        pass

    assert session.closed is False
