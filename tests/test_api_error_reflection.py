"""API error messages must never carry credentials.

A hostile or compromised Nexus endpoint controls every byte of a response body
and can echo request contents back. Those messages are reachable from CLI
output and from result fields, so anything the endpoint controls must not
survive into them.

The download-link case is the sharpest: that response carries the signed-link
key/expires pair, so reflecting it would put download credentials into an error
message.

These tests assert the credential property, which every line of this codebase
should satisfy, rather than how much non-secret endpoint text survives. That
last part is a deliberate design choice and differs between the stable line
(static category text) and the beta line (benign GraphQL error text survives
after secret and URL redaction); see the divergence note on the 0.2.2 work.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus_collection_dl.api import NexusAPI, NexusAPIError, NexusPremiumRequired

SENTINEL = "LEAK-MARKER-6c1f9a"
API_KEY = "test-api-key-SENTINEL"

# Stands in for anything an endpoint would want reflected: a signed-link
# credential pair, a path, an HTML body.
HOSTILE_BODY = {
    "key": SENTINEL,
    "expires": SENTINEL,
    "URI": f"https://example.invalid/{SENTINEL}",
}

FORBIDDEN = (SENTINEL, API_KEY, "expires=", "key=")


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        url: str = "https://api.invalid/x",
    ) -> None:
        self.status_code = status_code
        self.url = url + f"?key={SENTINEL}"
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body) if self._body else ""

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        raise AssertionError("should not be reached in these tests")


def _api() -> NexusAPI:
    api = NexusAPI.__new__(NexusAPI)
    api.api_key = API_KEY
    return api


def _message_of(exc_type: type[BaseException], fn) -> str:
    with pytest.raises(exc_type) as excinfo:
        fn()
    return str(excinfo.value)


def _assert_no_credentials(msg: str) -> None:
    for token in FORBIDDEN:
        assert token not in msg, f"{token!r} leaked into the error message: {msg!r}"


def test_403_body_never_surfaces() -> None:
    api = _api()
    msg = _message_of(
        NexusAPIError,
        lambda: api._handle_response(FakeResponse(403, HOSTILE_BODY)),
    )
    _assert_no_credentials(msg)


def test_403_premium_hint_detected_without_reflecting_body() -> None:
    api = _api()
    body = {"error": "PREMIUM membership is required"}
    msg = _message_of(
        NexusPremiumRequired,
        lambda: api._handle_response(FakeResponse(403, body)),
    )
    _assert_no_credentials(msg)


def test_404_request_url_query_never_surfaces() -> None:
    api = _api()
    msg = _message_of(
        NexusAPIError,
        lambda: api._handle_response(FakeResponse(404, {"detail": SENTINEL})),
    )
    _assert_no_credentials(msg)


def test_graphql_errors_never_surface_credentials() -> None:
    api = _api()

    class GqlResponse(FakeResponse):
        pass

    def run() -> None:
        response = GqlResponse(200, {"errors": [{"message": f"invalid apikey {API_KEY}"}]})
        response.status_code = 200
        api._handle_response = lambda r, **k: r.json()  # type: ignore[method-assign]
        api._rate_limit_wait = lambda: None  # type: ignore[method-assign]
        api.session = type("S", (), {"post": staticmethod(lambda *a, **k: response)})()  # type: ignore[assignment]
        api.graphql_query("{ x }")

    msg = _message_of(NexusAPIError, run)
    _assert_no_credentials(msg)


def test_download_link_error_never_surfaces_credentials() -> None:
    """The sharp case: the download-link payload carries key/expires."""
    api = _api()

    class BadShape(FakeResponse):
        pass

    def run() -> None:
        response = BadShape(200, HOSTILE_BODY)
        response.status_code = 200
        api._handle_response = lambda r, **k: r.json()  # type: ignore[method-assign]
        api._rate_limit_wait = lambda: None  # type: ignore[method-assign]
        api._request = lambda *a, **k: response  # type: ignore[method-assign]
        api.session = type("S", (), {"get": staticmethod(lambda *a, **k: response)})()  # type: ignore[assignment]
        api.get_download_url("skyrimspecialedition", 123, 456)

    msg = _message_of(NexusAPIError, run)
    _assert_no_credentials(msg)


def test_no_raise_site_interpolates_credential_bearing_content() -> None:
    """Source guard: response URLs and download-link payloads must not be
    interpolated raw into exception messages."""
    import inspect
    from pathlib import Path

    source = Path(inspect.getfile(NexusAPI)).read_text()
    offending = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if "NexusAPIError(" not in line and "NexusPremiumRequired(" not in line:
            continue
        window = "\n".join(source.splitlines()[lineno - 1 : lineno + 4])
        for token in ("{response.url}", "{data}", "{data.get"):
            if token in window:
                offending.append((lineno, token))
    assert not offending, f"API error messages interpolate credential-bearing content: {offending}"
