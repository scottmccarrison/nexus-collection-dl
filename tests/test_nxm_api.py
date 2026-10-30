"""API-layer tests for the signed download_link protocol and redaction.

Uses a fake session recording every call plus real requests.Response
objects, so the true _handle_response code paths run. Verifies: the
key/expires pair is forwarded as query params exactly as issued, user_id
is never an API parameter, status codes map to credential-safe errors,
malicious/reflected server text never surfaces secrets, an invalid pair
never silently falls back to an unsigned retry, and DEBUG HTTP logging is
suppressed for signed requests. No network.
"""

import json
import logging

import pytest
import requests

from nexus_collection_dl.api import (
    NexusAPI,
    NexusAPIError,
    NexusPremiumRequired,
    NexusRateLimited,
    redact_secret_values,
    redact_url,
    suppress_http_debug_logging,
)

GAME = "skyrimspecialedition"
KEY = "SignedKeyValue-abc123_XYZ"
EXPIRES = 1893456000
DL_PATH = f"/v1/games/{GAME}/mods/123/files/456/download_link.json"


def make_response(status=200, body=None, url="https://api.nexusmods.com/v1/x"):
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.reason = "Status"
    r.headers = {}
    if body is not None:
        r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    else:
        r._content = b""
    return r


class FakeSession:
    """Records calls; returns queued responses (or a default 200)."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return make_response(200, [{"URI": "https://cdn.example/file?token=t"}])

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return make_response(200, {})

    def delete(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return make_response(200, {})


@pytest.fixture()
def api():
    client = NexusAPI(api_key="test-api-key")
    client._min_request_interval = 0  # no sleeping in tests
    return client


def signed_url_with_query():
    return f"https://api.nexusmods.com{DL_PATH}?key={KEY}&expires={EXPIRES}"


# ---------------------------------------------------------------------------
# Protocol: pair forwarded, user_id never sent
# ---------------------------------------------------------------------------

def test_signed_pair_forwarded_as_query_params(api):
    api.session = FakeSession()
    uri = api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    assert uri == "https://cdn.example/file?token=t"
    url, kwargs = api.session.calls[0]
    assert url.endswith(DL_PATH)
    assert "?" not in url                       # pair goes via params, not raw URL
    assert kwargs["params"] == {"key": KEY, "expires": EXPIRES}
    # user_id is never an API query parameter (there is no such kwarg at all)
    assert "user_id" not in url
    assert set(kwargs["params"]) == {"key", "expires"}


def test_unsigned_premium_call_has_no_params(api):
    api.session = FakeSession()
    api.get_download_url(GAME, 123, 456)
    url, kwargs = api.session.calls[0]
    assert kwargs.get("params") is None


def test_half_pair_refused_without_request(api):
    api.session = FakeSession()
    with pytest.raises(NexusAPIError):
        api.get_download_url(GAME, 123, 456, key=KEY)
    with pytest.raises(NexusAPIError):
        api.get_download_url(GAME, 123, 456, expires=EXPIRES)
    assert api.session.calls == []              # zero requests made


def test_expired_pair_never_falls_back_to_unsigned(api):
    """A rejected pair fails the re-click: no silent unsigned retry."""
    api.session = FakeSession([make_response(400, {"message": "bad"})])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    assert len(api.session.calls) == 1          # exactly one request, no retry
    assert "Mod manager download" in str(excinfo.value)  # actionable re-click


# ---------------------------------------------------------------------------
# Status mapping and credential-safe messages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429, 500])
def test_status_codes_map_to_api_errors(api, status):
    resp = make_response(status, {"message": f"body {status}"}, url=signed_url_with_query())
    if status == 429:
        resp.headers = {"Retry-After": "30"}
    api.session = FakeSession([resp])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    message = str(excinfo.value)
    assert KEY not in message                   # credential never surfaces
    assert str(EXPIRES) not in message
    assert "key=" not in message                # signed query never surfaces
    if status == 429:
        assert isinstance(excinfo.value, NexusRateLimited)


def test_403_premium_hint_detected_privately(api):
    resp = make_response(403, {"error": "Premium membership required"}, url=signed_url_with_query())
    api.session = FakeSession([resp])
    with pytest.raises(NexusPremiumRequired):
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)


def test_403_without_premium_hint_stays_generic(api):
    resp = make_response(403, {"error": "forbidden"}, url=signed_url_with_query())
    api.session = FakeSession([resp])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    assert not isinstance(excinfo.value, NexusPremiumRequired)
    assert "403" in str(excinfo.value)


def test_malicious_reflected_body_and_url_are_redacted(api):
    """Server echoes the credential in body and URL: nothing may leak."""
    evil_body = {"message": f"invalid key {KEY} expires {EXPIRES} for you"}
    resp = make_response(400, evil_body, url=signed_url_with_query())
    api.session = FakeSession([resp])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    text = str(excinfo.value)
    assert KEY not in text
    assert str(EXPIRES) not in text
    assert "key=" not in text and "expires=" not in text


def test_404_message_carries_redacted_url_only(api):
    resp = make_response(404, {}, url=signed_url_with_query())
    api.session = FakeSession([resp])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    message = str(excinfo.value)
    assert "Resource not found" in message
    assert f"https://api.nexusmods.com{DL_PATH}" in message  # path kept, useful
    assert "?" not in message.split("Resource not found:")[1].split(" ")[0]


def test_unexpected_body_shape_error_is_sanitized(api):
    resp = make_response(200, {"URI": f"https://x/?key={KEY}"}, url=signed_url_with_query())
    api.session = FakeSession([resp])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    text = str(excinfo.value)
    assert KEY not in text
    assert "Unexpected download link response" in text


def test_existing_endpoints_unaffected(api):
    api.session = FakeSession([make_response(200, {"user_id": 7, "is_premium": True})])
    info = api.validate_key()
    assert info["user_id"] == 7
    api.session = FakeSession([make_response(200, {"files": [{"file_id": 1}]})])
    assert api.get_mod_files(GAME, 123)[0]["file_id"] == 1


# ---------------------------------------------------------------------------
# Redaction helpers and log suppression
# ---------------------------------------------------------------------------

def test_redact_url_strips_query_and_fragment():
    assert redact_url(signed_url_with_query() + "#f") == f"https://api.nexusmods.com{DL_PATH}"
    assert redact_url("not a url") == "not a url"
    assert redact_url("") == "[url redacted]"


def test_redact_secret_values_literal_and_url():
    text = f"boom {KEY} at https://cdn/x?key={KEY}&expires={EXPIRES} plus {EXPIRES}"
    out = redact_secret_values(text, (KEY, EXPIRES))
    assert KEY not in out and str(EXPIRES) not in out
    assert "https://" not in out
    # Short values are skipped (no mangling of unrelated text)
    assert redact_secret_values("id 42 ok", ("42",)) == "id 42 ok"


def test_debug_http_logging_suppressed_during_signed_request(api):
    seen = {}

    class RecordingSession(FakeSession):
        def get(self, url, **kwargs):
            seen["level"] = logging.getLogger("urllib3.connectionpool").getEffectiveLevel()
            return super().get(url, **kwargs)

    logger = logging.getLogger("urllib3.connectionpool")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        api.session = RecordingSession()
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
        assert seen["level"] >= logging.INFO     # lifted during the request
    finally:
        logger.setLevel(old_level)
    assert logger.level == old_level             # restored afterwards


def test_suppress_context_restores_levels_on_error():
    logger = logging.getLogger("urllib3")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(RuntimeError):
            with suppress_http_debug_logging():
                raise RuntimeError("boom")
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(old_level)
