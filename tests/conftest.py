"""Shared fixtures and test doubles.

The doubles here exist so unit tests never touch the network. Reused across test
modules rather than redefined per file: a fake that drifts between tests is worse
than no fake.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adp_forecast.config import FredSettings  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: A syntactically valid FRED key (32 alphanumeric chars) that is not a real one.
DUMMY_API_KEY = "a" * 32


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by file name (without extension)."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


class FakeResponse:
    """Minimal stand-in for :class:`requests.Response`.

    Implements only the surface the adapter touches, so a change in what the
    adapter relies on breaks these tests loudly instead of being absorbed by an
    over-permissive mock.
    """

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)

    def json(self) -> Any:
        """Return the parsed body, mirroring requests' ValueError on bad JSON."""
        if self._payload is None:
            raise ValueError("No JSON object could be decoded")
        return self._payload


class FakeSession:
    """Scripted :class:`requests.Session` replacement.

    Each queued item is either a :class:`FakeResponse` to return or an exception
    instance to raise, which lets one double cover success, HTTP error and
    transport-failure paths.

    Attributes:
        calls: Recorded ``(url, params)`` pairs, in order, for assertions about
            the parameters the adapter actually sent.
    """

    def __init__(self, responses: Iterable[FakeResponse | BaseException]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, params: dict[str, Any], timeout: float) -> FakeResponse:
        """Record the call and return (or raise) the next scripted item."""
        self.calls.append((url, dict(params)))
        if not self._responses:
            raise AssertionError(f"FakeSession exhausted; unexpected GET {url}")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def settings() -> FredSettings:
    """Settings with retries and backoff minimised so tests stay fast."""
    return FredSettings(
        api_key=DUMMY_API_KEY,
        max_retries=2,
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise retry backoff so retry tests do not spend wall-clock time."""
    monkeypatch.setattr("adp_forecast.retry._sleep", lambda _seconds: None)


@pytest.fixture
def timeout_error() -> requests.Timeout:
    """A transport timeout, as raised by requests."""
    return requests.Timeout("timed out")


@pytest.fixture
def connection_error() -> requests.ConnectionError:
    """A transport connection failure, as raised by requests."""
    return requests.ConnectionError("connection reset")
