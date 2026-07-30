"""FRED (Federal Reserve Economic Data) implementation of the ingestion ports.

Satisfies both :class:`~adp_forecast.ingestion.port.IngestionPort` and
:class:`~adp_forecast.ingestion.port.ReleaseCalendarPort`. All FRED-specific
knowledge — the host name, the ``"."`` missing-value encoding, the realtime
parameters, the error payload shape — is confined to this module.

Verified upstream behaviour this adapter is built around (checked live, not assumed):

* Host is ``api.stlouisfed.org/fred``. There is no ``api.fred.stlouisfed.org``.
* Errors are always ``HTTP 400`` with a JSON body carrying ``error_code`` and
  ``error_message`` — for a bad series ID *and* for a rejected key. Neither is
  retryable, hence the eager permanent-error classification below.
* Missing values arrive as the string ``"."``.
* A wide realtime window returns the complete revision history in range-compressed
  form: rows scale with the number of edits, not observations x vintages. All seven
  registered series together are ~18k rows in 7 requests, which is why full vintage
  ingestion is both cheaper and more informative than snapshotting per forecast date.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Final, Iterator, Mapping

import requests

from ..config import FredSettings
from ..domain import (
    CURRENT_VINTAGE_SENTINEL,
    EARLIEST_REALTIME,
    Observation,
)
from ..exceptions import (
    AuthenticationError,
    PermanentIngestionError,
    RateLimitError,
    ResponseValidationError,
    SeriesNotFoundError,
    TransientIngestionError,
)
from ..logging_config import get_logger
from ..retry import call_with_retry

_LOG = get_logger(__name__)

#: Value FRED substitutes for a missing observation.
_MISSING_VALUE_TOKEN: Final[str] = "."

_OBSERVATIONS_PATH: Final[str] = "series/observations"
_RELEASE_DATES_PATH: Final[str] = "release/dates"

#: Maximum ``limit`` each endpoint accepts. FRED does not use one global cap:
#: observations allow 100k while release dates reject anything over 10k with an
#: HTTP 400. Sending too large a value is a hard failure, not a clamp, so the page
#: size is looked up per endpoint rather than assumed.
_MAX_PAGE_SIZE: Final[dict[str, int]] = {
    _OBSERVATIONS_PATH: 100_000,
    _RELEASE_DATES_PATH: 10_000,
}

#: Conservative fallback for any endpoint added later without its own entry.
_DEFAULT_PAGE_SIZE: Final[int] = 1_000

#: Substrings FRED uses in ``error_message`` to distinguish a rejected key from
#: other bad-request causes. Matched case-insensitively.
_AUTH_ERROR_MARKERS: Final[tuple[str, ...]] = ("api_key", "api key")
_SERIES_ERROR_MARKERS: Final[tuple[str, ...]] = ("series_id", "does not exist")


class FredAdapter:
    """Fetches observations and release dates from the FRED REST API.

    A single instance reuses one :class:`requests.Session`, so the seven-request
    ingest pays one TLS handshake instead of seven. Instances are not thread-safe
    beyond what ``requests.Session`` guarantees; construct one per worker.

    Usable as a context manager to guarantee the session is closed::

        with FredAdapter(FredSettings.from_env()) as adapter:
            observations = adapter.fetch("ADPMNUSNERSA")
    """

    source_name: Final[str] = "FRED"

    def __init__(
        self,
        settings: FredSettings,
        session: requests.Session | None = None,
    ) -> None:
        """Initialise the adapter.

        Args:
            settings: Transport configuration including the API key.
            session: Injected HTTP session. Supplied by tests to avoid network
                access; when omitted a session is created and owned by this
                instance.
        """
        self._settings = settings
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._session.headers.setdefault("User-Agent", settings.user_agent)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "FredAdapter":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the HTTP session if this instance created it."""
        if self._owns_session:
            self._session.close()

    # -- IngestionPort -----------------------------------------------------

    def fetch(
        self,
        series_id: str,
        start: date | None = None,
        *,
        all_vintages: bool = False,
    ) -> list[Observation]:
        """Retrieve observations for one series.

        See :meth:`~adp_forecast.ingestion.port.IngestionPort.fetch` for the
        contract.

        Note:
            With ``all_vintages=False`` the returned realtime window is *not* a
            true vintage window. FRED reports it as "today..today" for a
            current-vintage query, so this adapter normalises it to
            ``today..CURRENT_VINTAGE_SENTINEL``, meaning "current as far as we
            know". Those records are fine for display but must not be fed to a
            point-in-time backtest — use ``all_vintages=True`` for that, which
            returns the real windows.
        """
        params: dict[str, Any] = {
            "series_id": series_id,
            "sort_order": "asc",
        }
        if start is not None:
            params["observation_start"] = start.isoformat()
        if all_vintages:
            # Widest possible realtime window: asks FRED for every revision,
            # range-compressed.
            params["realtime_start"] = EARLIEST_REALTIME.isoformat()
            params["realtime_end"] = CURRENT_VINTAGE_SENTINEL.isoformat()

        _LOG.info(
            "Fetching %s from FRED (start=%s, all_vintages=%s)",
            series_id,
            start.isoformat() if start else "beginning",
            all_vintages,
        )

        fetched_at = datetime.now(timezone.utc)
        rows = list(self._paginate(_OBSERVATIONS_PATH, params, "observations"))
        observations = [
            self._to_observation(row, series_id, fetched_at, all_vintages)
            for row in rows
        ]

        missing = sum(1 for obs in observations if obs.is_missing)
        if missing:
            _LOG.warning(
                "%s: %d of %d observations are missing upstream ('.')",
                series_id,
                missing,
                len(observations),
            )
        _LOG.info("%s: retrieved %d observations", series_id, len(observations))
        return observations

    # -- ReleaseCalendarPort ----------------------------------------------

    def fetch_release_dates(
        self,
        release_id: int,
        start: date | None = None,
    ) -> list[date]:
        """Retrieve actual publication dates for a FRED release.

        See :meth:`~adp_forecast.ingestion.port.ReleaseCalendarPort.fetch_release_dates`.

        Warning:
            FRED includes *scheduled future* release dates, not just past ones — a
            2024 start date currently returns dates into December 2026. Callers using
            these as backtest forecast origins must filter to dates that have already
            occurred, or they will generate origins for months with no actual data.

        Note:
            An unrecognised ``release_id`` returns HTTP 200 with an empty list rather
            than an error, so a typo cannot be distinguished by status code. An empty
            result is therefore logged as a warning.
        """
        params: dict[str, Any] = {
            "release_id": release_id,
            "sort_order": "asc",
            # Without this, FRED omits dates on which the release published no
            # new data, which would silently drop valid forecast origins.
            "include_release_dates_with_no_data": "true",
        }
        if start is not None:
            params["realtime_start"] = start.isoformat()

        _LOG.info("Fetching release dates for release_id=%d", release_id)
        rows = self._paginate(_RELEASE_DATES_PATH, params, "release_dates")
        dates = [self._parse_date(row["date"], "release date") for row in rows]
        if not dates:
            _LOG.warning(
                "release_id=%d returned no release dates. FRED answers an unknown "
                "release with an empty list rather than an error, so verify the ID.",
                release_id,
            )
        else:
            _LOG.info(
                "release_id=%d: retrieved %d release dates (%s..%s)",
                release_id,
                len(dates),
                dates[0].isoformat(),
                dates[-1].isoformat(),
            )
        return dates

    # -- HTTP plumbing -----------------------------------------------------

    def _paginate(
        self,
        path: str,
        params: Mapping[str, Any],
        payload_key: str,
    ) -> Iterator[dict[str, Any]]:
        """Yield every row for a request, following FRED's offset pagination.

        FRED caps a response at an endpoint-specific row count and reports the full
        match count in ``count``. Trusting a single response would silently truncate
        a large history, so this walks offsets until every row is collected.

        Time is O(n) in total rows; memory is one page at a time from this
        generator's perspective, though callers that materialise a list hold O(n).

        Args:
            path: API path below the base URL.
            params: Query parameters, excluding pagination and credentials.
            payload_key: Key in the JSON body holding the row list.

        Yields:
            Raw row dictionaries in upstream order.
        """
        page_size = _MAX_PAGE_SIZE.get(path, _DEFAULT_PAGE_SIZE)
        offset = 0
        total: int | None = None
        seen = 0

        while True:
            page_params = dict(params)
            page_params["limit"] = page_size
            page_params["offset"] = offset
            payload = self._request(path, page_params)

            rows = payload.get(payload_key)
            if not isinstance(rows, list):
                raise ResponseValidationError(
                    f"FRED response for '{path}' has no '{payload_key}' list; "
                    f"got keys {sorted(payload)}"
                )

            yield from rows
            seen += len(rows)

            if total is None:
                total = payload.get("count") if isinstance(payload.get("count"), int) else seen
            if not rows or seen >= total:
                break
            offset += len(rows)

    def _request(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Perform one API call with the configured retry policy applied."""
        return call_with_retry(
            lambda: self._request_once(path, params),
            max_retries=self._settings.max_retries,
            backoff_base_seconds=self._settings.backoff_base_seconds,
            backoff_max_seconds=self._settings.backoff_max_seconds,
            description=f"GET {path} {_redact(params)}",
        )

    def _request_once(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Perform a single attempt, translating failures into package exceptions."""
        url = f"{self._settings.base_url}/{path}"
        query = dict(params)
        query["api_key"] = self._settings.api_key
        query["file_type"] = "json"

        try:
            response = self._session.get(
                url,
                params=query,
                timeout=self._settings.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise TransientIngestionError(
                f"Timed out after {self._settings.timeout_seconds}s: GET {path}"
            ) from exc
        except requests.ConnectionError as exc:
            raise TransientIngestionError(f"Connection failed: GET {path}") from exc
        except requests.RequestException as exc:
            # Anything else from requests is a client-side defect (bad URL, invalid
            # params); retrying will not fix it.
            raise PermanentIngestionError(f"Request failed: GET {path}: {exc}") from exc

        if response.status_code != 200:
            raise self._classify_error(response, path)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ResponseValidationError(
                f"FRED returned non-JSON for GET {path} "
                f"(status {response.status_code}): {response.text[:200]!r}"
            ) from exc

        if not isinstance(payload, dict):
            raise ResponseValidationError(
                f"FRED returned {type(payload).__name__}, expected object: GET {path}"
            )
        return payload

    def _classify_error(self, response: requests.Response, path: str) -> Exception:
        """Map an HTTP failure onto the transient/permanent exception split.

        The classification drives retry behaviour, so it is deliberately explicit
        rather than "retry anything that is not 2xx". FRED answers a typo'd series
        ID and a rejected key with the same HTTP 400, so the JSON ``error_message``
        is inspected to produce an actionable exception type.
        """
        status = response.status_code
        message = self._extract_error_message(response)
        context = f"GET {path} -> HTTP {status}: {message}"

        if status == 429:
            return RateLimitError(f"FRED rate limit hit. {context}")
        if status >= 500:
            return TransientIngestionError(f"FRED server error. {context}")

        if status == 400:
            lowered = message.lower()
            if any(marker in lowered for marker in _AUTH_ERROR_MARKERS):
                return AuthenticationError(
                    "FRED rejected the API key. Check FRED_API_KEY in .env. "
                    f"{context}"
                )
            if any(marker in lowered for marker in _SERIES_ERROR_MARKERS):
                return SeriesNotFoundError(f"FRED rejected the series/release. {context}")

        return PermanentIngestionError(f"FRED rejected the request. {context}")

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        """Pull ``error_message`` from a FRED error body, falling back to raw text."""
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(payload, dict):
            return str(payload.get("error_message", payload))[:300]
        return str(payload)[:300]

    # -- parsing -----------------------------------------------------------

    def _to_observation(
        self,
        row: Mapping[str, Any],
        series_id: str,
        fetched_at: datetime,
        all_vintages: bool,
    ) -> Observation:
        """Convert one raw FRED row into an :class:`Observation`.

        Args:
            row: Raw row from the ``observations`` list.
            series_id: Series the row belongs to. Taken from the request rather
                than the payload, which does not repeat it per row.
            fetched_at: Retrieval timestamp, shared across the batch so every
                record from one call carries identical provenance.
            all_vintages: Whether the request asked for full revision history,
                which determines whether the realtime window is meaningful.

        Raises:
            ResponseValidationError: If a required field is absent or malformed.
        """
        try:
            raw_date = row["date"]
            raw_value = row["value"]
        except KeyError as exc:
            raise ResponseValidationError(
                f"FRED observation for {series_id} is missing field {exc}: {dict(row)!r}"
            ) from exc

        realtime_start = self._parse_date(
            row.get("realtime_start"), f"{series_id} realtime_start"
        )
        if all_vintages:
            realtime_end = self._parse_date(
                row.get("realtime_end"), f"{series_id} realtime_end"
            )
        else:
            # A current-vintage query reports today for both bounds; normalise the
            # upper bound to the open-ended sentinel so `is_current_vintage` is true
            # and `known_on` cannot be misread as a real vintage window.
            realtime_end = CURRENT_VINTAGE_SENTINEL

        return Observation(
            series_id=series_id,
            date=self._parse_date(raw_date, f"{series_id} observation date"),
            value=self._parse_value(raw_value, series_id, raw_date),
            source=self.source_name,
            fetched_at=fetched_at,
            realtime_start=realtime_start,
            realtime_end=realtime_end,
        )

    @staticmethod
    def _parse_value(raw: Any, series_id: str, raw_date: Any) -> float | None:
        """Coerce a FRED value string to a float, mapping ``"."`` to ``None``.

        Raises:
            ResponseValidationError: If the value is neither ``"."`` nor numeric.
                Silently nulling an unparseable value would let a units change or
                a payload format change pass as missing data.
        """
        if raw is None:
            return None
        text = str(raw).strip()
        if text == _MISSING_VALUE_TOKEN or not text:
            return None
        try:
            return float(text)
        except ValueError as exc:
            raise ResponseValidationError(
                f"{series_id} @ {raw_date}: value {raw!r} is neither numeric nor "
                f"the missing-value token {_MISSING_VALUE_TOKEN!r}"
            ) from exc

    @staticmethod
    def _parse_date(raw: Any, context: str) -> date:
        """Parse a FRED ``YYYY-MM-DD`` date.

        Raises:
            ResponseValidationError: If absent or not ISO-formatted.
        """
        if raw is None:
            raise ResponseValidationError(f"{context}: date is absent")
        try:
            return date.fromisoformat(str(raw))
        except ValueError as exc:
            raise ResponseValidationError(
                f"{context}: {raw!r} is not an ISO date"
            ) from exc


def _redact(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return params with any credential removed, for safe logging."""
    return {key: value for key, value in params.items() if key != "api_key"}
