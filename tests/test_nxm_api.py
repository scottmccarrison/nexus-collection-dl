"""API-layer tests for the signed download_link protocol and redaction.

Uses a fake session recording every call plus real requests.Response
objects, so the true _handle_response code paths run. Verifies: the
key/expires pair is forwarded as query params exactly as issued, user_id
is never an API parameter, status codes map to credential-safe errors,
malicious/reflected server text never surfaces secrets (including in
GraphQL error payloads), an invalid pair never silently falls back to an
unsigned retry, and DEBUG HTTP logging is suppressed for signed
requests. No network.
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


def test_graphql_errors_redact_reflected_secrets(api):
    """Server-controlled GraphQL error text is redacted before it is
    embedded in the exception: a reflected apikey never surfaces and
    residual URLs are stripped, while benign text survives."""

    class GraphqlSession(FakeSession):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return make_response(200, {"errors": [
                {"message": "invalid apikey test-api-key"},
                {"message": f"retry at https://x.example/?key={KEY}"},
                {"message": "field 'collection' is unknown"},
            ]})

    api.session = GraphqlSession()
    with pytest.raises(NexusAPIError) as excinfo:
        api.graphql_query('{ collection(slug: "x") { id } }')
    text = str(excinfo.value)
    assert "GraphQL errors" in text
    assert "test-api-key" not in text          # reflected apikey redacted
    assert KEY not in text
    assert "https://" not in text              # residual URLs stripped
    assert "[redacted]" in text
    assert "field 'collection' is unknown" in text  # benign text survives


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
    # Explicitly provided secrets are redacted at ANY length (fail-safe);
    # only None/empty/whitespace values are skipped.
    assert redact_secret_values("id 42 ok", ("42",)) == "id [redacted] ok"


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


# ---------------------------------------------------------------------------
# Bugcheck regressions: redaction lengths, non-JSON bodies, redirects,
# timeouts, and thread-safe log suppression
# ---------------------------------------------------------------------------

import threading  # noqa: E402

from nexus_collection_dl.api import (  # noqa: E402
    SIGNED_REQUEST_TIMEOUT,
    redact_literal_values,
)


def test_provided_secrets_redacted_regardless_of_length():
    text = "reflected k9x in /path/k9x?q=k9x"
    out = redact_secret_values(text, ("k9x",))
    assert "k9x" not in out
    assert out.count("[redacted]") == 3
    # None/empty/whitespace secrets never mangle text
    assert redact_literal_values("keep me", ("", "  ", None)) == "keep me"


def test_nonjson_2xx_becomes_domain_error_without_echo(api):
    html = b"<html><body>intercepted</body></html>"
    api.session = FakeSession([make_response(200, html, url=signed_url_with_query())])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    msg = str(excinfo.value)
    assert "non-JSON" in msg
    assert "<html>" not in msg
    assert KEY not in msg and str(EXPIRES) not in msg
    assert "https://" not in msg


def test_nonjson_2xx_on_plain_endpoint(api):
    api.session = FakeSession([make_response(200, b"<html>portal</html>")])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_mod_files(GAME, 123)
    msg = str(excinfo.value)
    assert "non-JSON" in msg and "<html>" not in msg


def test_signed_request_refuses_redirects_and_is_time_bounded(api):
    api.session = FakeSession([make_response(302)])
    with pytest.raises(NexusAPIError) as excinfo:
        api.get_download_url(GAME, 123, 456, key=KEY, expires=EXPIRES)
    assert len(api.session.calls) == 1              # redirect never followed
    _url, kwargs = api.session.calls[0]
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == SIGNED_REQUEST_TIMEOUT
    msg = str(excinfo.value)
    assert KEY not in msg and str(EXPIRES) not in msg


def test_suppress_context_nested_same_thread_restores_only_outermost():
    logger = logging.getLogger("requests")
    old = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with suppress_http_debug_logging():
            assert logger.level >= logging.INFO
            with suppress_http_debug_logging():
                assert logger.level >= logging.INFO
            assert logger.level >= logging.INFO     # inner exit keeps the lift
        assert logger.level == logging.DEBUG        # outermost restores exactly
    finally:
        logger.setLevel(old)


def test_suppress_contexts_serialize_across_threads_without_debug_leaks():
    logger = logging.getLogger("urllib3")
    old = logger.level
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    secret_line = f"GET https://cdn.example/f?key={KEY}&expires={EXPIRES}"
    a_in = threading.Event()
    a_release = threading.Event()
    b_inside = threading.Event()
    errors = []

    def worker_a():
        try:
            with suppress_http_debug_logging():
                a_in.set()
                if not a_release.wait(timeout=10):
                    errors.append(RuntimeError("A stalled"))
                logger.debug(secret_line)           # must be suppressed
        except Exception as e:
            errors.append(e)

    def worker_b():
        try:
            if not a_in.wait(timeout=10):
                errors.append(RuntimeError("B stalled"))
                return
            with suppress_http_debug_logging():     # must block until A is done
                b_inside.set()
                logger.debug(secret_line)
        except Exception as e:
            errors.append(e)

    try:
        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        ta.start()
        tb.start()
        assert a_in.wait(timeout=10)
        # While A holds the context, B cannot enter and levels stay lifted
        assert not b_inside.wait(timeout=0.3)
        assert logger.level >= logging.INFO
        a_release.set()
        ta.join(timeout=10)
        tb.join(timeout=10)
        assert not ta.is_alive() and not tb.is_alive()
        assert not errors
        assert b_inside.is_set()                    # B ran after A completed
        # Neither protected section produced a DEBUG record carrying secrets
        assert not any(KEY in m or str(EXPIRES) in m for m in records)
        # Exact restoration once every context has exited
        assert logger.level == logging.DEBUG
        # Suppression is scoped: outside contexts DEBUG flows again
        records.clear()
        logger.debug(secret_line)
        assert any(KEY in m for m in records)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)
