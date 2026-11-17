"""API error messages must not reflect response content.

A hostile or compromised Nexus endpoint controls every byte of a response body
and can echo request contents back. Those messages are reachable from CLI
output and from web task results, so an exception that interpolates `{data}` or
`{response.url}` turns the endpoint into a reflection surface.

The download-link case is the sharpest: that response carries the signed-link
key/expires pair, so reflecting it would put download credentials into an error
message.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus_collection_dl.api import NexusAPI, NexusAPIError, NexusPremiumRequired

SENTINEL = "LEAK-MARKER-6c1f9a"

# Stands in for anything an endpoint would want reflected: a signed-link
# credential pair, a path, an HTML body.
HOSTILE_BODY = {
    "key": SENTINEL,
    "expires": SENTINEL,
    "URI": f"https://example.invalid/{SENTINEL}",
}


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, url: str = "https://api.invalid/x") -> None:
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
    api.api_key = "unused"
    return api


def _message_of(exc_type: type[BaseException], fn) -> str:
    with pytest.raises(exc_type) as excinfo:
        fn()
    return str(excinfo.value)


def test_403_does_not_reflect_the_response_body() -> None:
    api = _api()
    msg = _message_of(
        NexusAPIError,
        lambda: api._handle_response(FakeResponse(403, HOSTILE_BODY)),
    )
    assert SENTINEL not in msg


def test_403_premium_hint_still_detected_without_reflecting_body() -> None:
    api = _api()
    body = {"error": "PREMIUM membership is required"}
    msg = _message_of(
        NexusPremiumRequired,
        lambda: api._handle_response(FakeResponse(403, body)),
    )
    assert SENTINEL not in msg
    assert "PREMIUM membership is required" not in msg


def test_404_does_not_reflect_the_request_url() -> None:
    api = _api()
    msg = _message_of(
        NexusAPIError,
        lambda: api._handle_response(FakeResponse(404, {"detail": SENTINEL})),
    )
    assert SENTINEL not in msg


def test_graphql_errors_do_not_reflect_the_payload() -> None:
    api = _api()

    class GqlResponse(FakeResponse):
        pass

    def run() -> None:
        response = GqlResponse(200, {"errors": [{"message": SENTINEL}]})
        response.status_code = 200
        # bypass _handle_response's status handling by patching it
        api._handle_response = lambda r: r.json()  # type: ignore[method-assign]
        api._rate_limit_wait = lambda: None  # type: ignore[method-assign]
        api.session = type("S", (), {"post": staticmethod(lambda *a, **k: response)})()  # type: ignore[assignment]
        api.graphql_query("{ x }")

    msg = _message_of(NexusAPIError, run)
    assert SENTINEL not in msg


def test_download_link_error_does_not_reflect_credentials() -> None:
    """The sharp case: the download-link payload carries key/expires."""
    api = _api()

    class BadShape(FakeResponse):
        pass

    def run() -> None:
        # A response that is not the expected list of {URI} dicts, holding a
        # credential pair an endpoint reflected back.
        response = BadShape(200, HOSTILE_BODY)
        response.status_code = 200
        api._handle_response = lambda r: r.json()  # type: ignore[method-assign]
        api._rate_limit_wait = lambda: None  # type: ignore[method-assign]
        api.session = type("S", (), {"get": staticmethod(lambda *a, **k: response)})()  # type: ignore[assignment]
        api.get_download_url("skyrimspecialedition", 123, 456)

    msg = _message_of(NexusAPIError, run)
    assert SENTINEL not in msg
    assert "key" not in msg
    assert "expires" not in msg
    # identifiers survive, so the failure is still actionable
    assert "123" in msg and "456" in msg


def test_no_raise_site_interpolates_a_response_object() -> None:
    """Source-level guard: no NexusAPIError message may embed response data."""
    import inspect
    from pathlib import Path

    source = Path(inspect.getfile(NexusAPI)).read_text()
    offending = []
    for lineno, line in enumerate(source.splitlines(), 1):
        if "NexusAPIError(" not in line and "NexusPremiumRequired(" not in line:
            continue
        window = "\n".join(source.splitlines()[lineno - 1 : lineno + 3])
        for token in ("{data}", "{response.url}", "{data[", "{data.get"):
            if token in window:
                offending.append((lineno, token))
    assert not offending, f"API error messages reflect response content: {offending}"
