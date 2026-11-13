"""Manifest fetch transport failures never expose request URLs.

Both manifest fetch sites (the download_link lookup and the signed CDN
bundle download) run through requests, whose exception text embeds the
full request URL. A bundle URL carries signing query parameters
(md5/expires/token), the credential class the downloader and api
modules wrap categorically, and the service callers catch ManifestError
only, so a raw transport error must never reach them: it becomes
ManifestError with categorical text that carries neither the URL nor
its query values. Fake secrets only, asserted absent.
"""

import pytest
import requests

from nexus_collection_dl import manifest as manifest_module
from nexus_collection_dl.manifest import (
    ManifestError,
    download_and_parse_manifest,
)

FAKE_QUERY_SECRET = "FAKEQUERYSECRET99"


class FakeLinkResponse:
    """Successful download_link response naming a signed CDN URL."""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "download_links": [
                {
                    "URI": (
                        "https://cdn.example/bundle.7z?md5=abc&expires=1"
                        f"&token={FAKE_QUERY_SECRET}"
                    )
                }
            ]
        }


class FakeLinkSession:
    """Session whose download_link lookup succeeds."""

    def get(self, url, **kwargs):
        return FakeLinkResponse()


class RaisingLinkSession:
    """Session whose download_link lookup fails at the transport layer."""

    def get(self, url, **kwargs):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='api.nexusmods.com'): Max retries "
            f"exceeded with url: {url}?token={FAKE_QUERY_SECRET}"
        )


class FakeBodyResponse:
    """download_link response whose body fails to parse as expected."""

    def __init__(self, json_result):
        self._json_result = json_result

    def raise_for_status(self):
        return None

    def json(self):
        if isinstance(self._json_result, Exception):
            raise self._json_result
        return self._json_result


class FakeBodySession:
    """Session whose download_link lookup returns a canned body."""

    def __init__(self, json_result):
        self._response = FakeBodyResponse(json_result)

    def get(self, url, **kwargs):
        return self._response


def assert_url_free(excinfo):
    """The wrapped message carries no URL and no query secret."""
    message = str(excinfo.value)
    assert "http" not in message.lower()
    assert FAKE_QUERY_SECRET not in message
    # The raw requests message is where the URL lives, so it must not
    # ride along as the cause of the ManifestError either.
    assert excinfo.value.__cause__ is None


def test_link_lookup_transport_failure_is_wrapped():
    with pytest.raises(ManifestError) as excinfo:
        download_and_parse_manifest(
            "/v2/collections/x/download_link", session=RaisingLinkSession()
        )
    assert "manifest download failed" in str(excinfo.value)
    assert_url_free(excinfo)


def test_bundle_download_transport_failure_is_wrapped(monkeypatch):
    # The second fetch site (module-level requests.get) raises with a
    # URL-bearing message containing the signed bundle query.

    def boom(url, **kwargs):
        raise requests.ConnectionError(
            f"Max retries exceeded with url: {url}&token={FAKE_QUERY_SECRET}"
        )

    monkeypatch.setattr(manifest_module.requests, "get", boom)
    with pytest.raises(ManifestError) as excinfo:
        download_and_parse_manifest(
            "/v2/collections/x/download_link", session=FakeLinkSession()
        )
    assert "manifest download failed" in str(excinfo.value)
    assert_url_free(excinfo)


def test_bundle_status_failure_is_wrapped(monkeypatch):
    # raise_for_status at the bundle fetch site gets the same wrap.

    class FakeResponse:
        def raise_for_status(self):
            raise requests.HTTPError(
                "403 Client Error for url: https://cdn.example/bundle.7z"
                f"?md5=abc&expires=1&token={FAKE_QUERY_SECRET}"
            )

    monkeypatch.setattr(
        manifest_module.requests, "get", lambda url, **kwargs: FakeResponse()
    )
    with pytest.raises(ManifestError) as excinfo:
        download_and_parse_manifest(
            "/v2/collections/x/download_link", session=FakeLinkSession()
        )
    assert "manifest download failed" in str(excinfo.value)
    assert_url_free(excinfo)


def test_link_lookup_non_json_body_is_wrapped():
    # requests.JSONDecodeError subclasses RequestException but is a body
    # parse failure: it must surface as ManifestError, not a raw
    # requests traceback.
    session = FakeBodySession(
        requests.exceptions.JSONDecodeError(
            "Expecting value: line 1 column 1 (char 0)", "<html>nope", 0
        )
    )
    with pytest.raises(ManifestError) as excinfo:
        download_and_parse_manifest(
            "/v2/collections/x/download_link", session=session
        )
    assert "manifest download failed" in str(excinfo.value)
    assert_url_free(excinfo)


@pytest.mark.parametrize(
    "body", [{}, {"download_links": []}, {"download_links": None}]
)
def test_link_lookup_missing_links_is_wrapped(body):
    with pytest.raises(ManifestError) as excinfo:
        download_and_parse_manifest(
            "/v2/collections/x/download_link", session=FakeBodySession(body)
        )
    message = str(excinfo.value)
    assert "manifest download failed" in message
    # The endpoint-controlled api path is never interpolated (finding 3):
    # the old "No download links returned from {api_url}" quoted it.
    assert "download_link" not in message
    assert_url_free(excinfo)


@pytest.mark.parametrize(
    "body",
    [
        ["not", "an", "object"],
        {"download_links": [{"no_uri": 1}]},
        {"download_links": [{"URI": None}]},
        {"download_links": ["https://cdn.example/bundle.7z"]},
    ],
)
def test_link_lookup_malformed_link_entry_is_wrapped(body):
    # Non-dict JSON, a link entry without "URI", a non-string URI, and a
    # non-mapping link entry: none may escape as AttributeError,
    # KeyError, or TypeError.
    with pytest.raises(ManifestError) as excinfo:
        download_and_parse_manifest(
            "/v2/collections/x/download_link", session=FakeBodySession(body)
        )
    assert "manifest download failed" in str(excinfo.value)
    assert_url_free(excinfo)
