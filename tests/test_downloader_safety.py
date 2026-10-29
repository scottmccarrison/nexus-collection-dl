"""Downloader safety tests: filename validation, integrity, error hygiene.

The network is fully mocked: a fake session scripts responses, and a fake
API records calls so we can prove no extra API calls happen for MD5.
"""

import hashlib
import os
from pathlib import Path

import pytest
import requests

from nexus_collection_dl import downloader as downloader_module
from nexus_collection_dl.downloader import (
    DownloadError,
    Downloader,
    _expected_md5,
    _expected_size,
    validate_download_filename,
)

SIGNED_URL = "https://cdn.nexus.example/files/download?token=SUPERSECRET&sig=abc"


class FakeAPI:
    def __init__(self):
        self.download_url_calls = 0
        self.other_calls = 0

    def get_download_url(self, game_domain, mod_id, file_id):
        self.download_url_calls += 1
        return SIGNED_URL


class FakeResponse:
    def __init__(self, content=b"payload", status=200, headers=None, fail_after=None):
        self.content = content
        self.status_code = status
        self.headers = headers if headers is not None else {
            "content-length": str(len(content))
        }
        self.fail_after = fail_after  # raise mid-stream after N bytes
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(
                f"{self.status_code} Client Error for url: {SIGNED_URL}"
            )
            error.response = self
            raise error

    def iter_content(self, chunk_size=8192):
        # Yield in small pieces so fail_after can trigger mid-stream
        step = min(chunk_size, 512)
        sent = 0
        for i in range(0, len(self.content), step):
            chunk = self.content[i:i + step]
            if self.fail_after is not None and sent >= self.fail_after:
                raise requests.ConnectionError(f"connection dropped mid-stream {SIGNED_URL}")
            sent += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, get_exc=None):
        self.response = response
        self.get_exc = get_exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.get_exc is not None:
            raise self.get_exc
        return self.response


def _make_downloader(response=None, get_exc=None):
    api = FakeAPI()
    d = Downloader(api)
    d.session = FakeSession(response=response, get_exc=get_exc)
    return d, api


def _mod(filename="Mod-1.0.zip", size=None, md5=None):
    info = {"mod_id": 1, "file_id": 2, "filename": filename}
    if size is not None:
        info["size_bytes"] = size
    if md5 is not None:
        info["expected_md5"] = md5
    return info


# ---------------------------------------------------------------------------
# Filename validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "../../evil.zip",
    "../evil.zip",
    "/etc/passwd",
    "sub/dir/mod.zip",
    "sub\\dir\\mod.zip",
    ".",
    "..",
    ".hidden.zip",
    "C:mod.zip",
    "mo\x00d.zip",
    "mod\n.zip",
    "",
])
def test_unsafe_filenames_rejected_before_any_api_call(bad, tmp_path):
    d, api = _make_downloader(response=FakeResponse(b"x"))
    with pytest.raises(DownloadError):
        d.download_mod("skyrim", _mod(filename=bad), tmp_path)
    assert api.download_url_calls == 0  # refused before wasting an API call
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere


def test_safe_filename_accepted(tmp_path):
    name = validate_download_filename("Mod Name-1.0 (final).zip")
    assert name == "Mod Name-1.0 (final).zip"


# ---------------------------------------------------------------------------
# Happy path and atomic publish
# ---------------------------------------------------------------------------

def test_successful_download_publishes_and_cleans_up(tmp_path):
    payload = b"archive bytes"
    resp = FakeResponse(payload)
    d, api = _make_downloader(response=resp)
    final = d.download_mod("skyrim", _mod(size=len(payload)), tmp_path)

    assert final == tmp_path / "Mod-1.0.zip"
    assert final.read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir()] == ["Mod-1.0.zip"]  # no temp leftovers
    assert resp.closed
    url, kwargs = d.session.calls[0]
    assert kwargs.get("timeout") == (
        downloader_module.CONNECT_TIMEOUT, downloader_module.READ_TIMEOUT
    )
    assert kwargs.get("stream") is True


def test_publish_is_same_directory_rename(tmp_path, monkeypatch):
    renames = []
    real_replace = os.replace

    def recording_replace(src, dst, **kwargs):
        renames.append((Path(str(src)).parent, Path(str(dst)).parent))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)
    payload = b"archive bytes"
    d, api = _make_downloader(response=FakeResponse(payload))
    d.download_mod("skyrim", _mod(size=len(payload)), tmp_path)
    assert renames and all(a == b for a, b in renames)


def test_md5_verified_from_mod_info_without_extra_api_calls(tmp_path):
    payload = b"archive bytes"
    good_md5 = hashlib.md5(payload).hexdigest()
    d, api = _make_downloader(response=FakeResponse(payload))
    final = d.download_mod("skyrim", _mod(size=len(payload), md5=good_md5), tmp_path)
    assert final.read_bytes() == payload
    assert api.download_url_calls == 1  # no extra API call to fetch the hash


# ---------------------------------------------------------------------------
# Integrity failures keep the existing final file
# ---------------------------------------------------------------------------

def test_size_mismatch_rejected_final_preserved(tmp_path):
    final = tmp_path / "Mod-1.0.zip"
    final.write_bytes(b"PREVIOUS GOOD DOWNLOAD")
    truncated = b"short"
    d, api = _make_downloader(response=FakeResponse(truncated))
    with pytest.raises(DownloadError) as excinfo:
        d.download_mod("skyrim", _mod(size=999), tmp_path)
    assert "size mismatch" in str(excinfo.value)
    assert final.read_bytes() == b"PREVIOUS GOOD DOWNLOAD"
    assert [p.name for p in tmp_path.iterdir()] == ["Mod-1.0.zip"]


def test_md5_mismatch_rejected_final_preserved(tmp_path):
    final = tmp_path / "Mod-1.0.zip"
    final.write_bytes(b"PREVIOUS GOOD DOWNLOAD")
    d, api = _make_downloader(response=FakeResponse(b"corrupt payload"))
    with pytest.raises(DownloadError) as excinfo:
        d.download_mod("skyrim", _mod(md5="0" * 32), tmp_path)
    assert "MD5 mismatch" in str(excinfo.value)
    assert final.read_bytes() == b"PREVIOUS GOOD DOWNLOAD"


def test_unknown_size_skips_check(tmp_path):
    payload = b"whatever length"
    d, api = _make_downloader(response=FakeResponse(payload))
    final = d.download_mod("skyrim", _mod(size=None), tmp_path)
    assert final.read_bytes() == payload
    d2, _ = _make_downloader(response=FakeResponse(payload))
    assert d2.download_mod("skyrim", _mod(size=0), tmp_path).read_bytes() == payload


def test_malformed_md5_skipped(tmp_path):
    payload = b"whatever"
    d, api = _make_downloader(response=FakeResponse(payload))
    final = d.download_mod("skyrim", _mod(md5="not-a-hash"), tmp_path)
    assert final.read_bytes() == payload


def test_expected_metadata_parsing():
    # Legacy KB sizes must never be treated as exact bytes: only size_bytes
    # (true byte counts) are enforced; >0 only.
    assert _expected_size({"size_bytes": 123}) == 123
    assert _expected_size({"size_bytes": "456"}) == 456
    assert _expected_size({"size_bytes": 0}) is None
    assert _expected_size({"size_bytes": None}) is None
    assert _expected_size({"size": 9999}) is None  # legacy KB field ignored
    assert _expected_size({}) is None
    assert _expected_md5({"expected_md5": "A" * 32}) == "a" * 32
    assert _expected_md5({"expected_md5": "short"}) is None
    assert _expected_md5({"expected_md5": None}) is None


# ---------------------------------------------------------------------------
# Transport failures: timeouts, HTTP errors, mid-stream drops
# ---------------------------------------------------------------------------

def test_http_error_message_never_leaks_signed_url(tmp_path):
    d, api = _make_downloader(response=FakeResponse(b"", status=403))
    with pytest.raises(DownloadError) as excinfo:
        d.download_mod("skyrim", _mod(), tmp_path)
    message = str(excinfo.value)
    assert "HTTP error 403" in message
    assert "SUPERSECRET" not in message
    assert SIGNED_URL not in message
    assert "https://" not in message
    assert list(tmp_path.iterdir()) == []


def test_timeout_message_never_leaks_signed_url(tmp_path):
    exc = requests.ConnectTimeout(f"connect timed out to {SIGNED_URL}")
    d, api = _make_downloader(get_exc=exc)
    with pytest.raises(DownloadError) as excinfo:
        d.download_mod("skyrim", _mod(), tmp_path)
    message = str(excinfo.value)
    assert "timed out" in message
    assert "SUPERSECRET" not in message
    assert SIGNED_URL not in message


def test_midstream_failure_cleans_temp_and_preserves_final(tmp_path):
    final = tmp_path / "Mod-1.0.zip"
    final.write_bytes(b"PREVIOUS GOOD DOWNLOAD")
    resp = FakeResponse(b"a" * 5000, fail_after=1000)
    d, api = _make_downloader(response=resp)
    with pytest.raises(DownloadError) as excinfo:
        d.download_mod("skyrim", _mod(), tmp_path)
    assert resp.closed  # streamed response closed on failure
    assert final.read_bytes() == b"PREVIOUS GOOD DOWNLOAD"
    assert [p.name for p in tmp_path.iterdir()] == ["Mod-1.0.zip"]
    assert "SUPERSECRET" not in str(excinfo.value)


def test_no_download_url_from_api(tmp_path):
    class NoUrlAPI(FakeAPI):
        def get_download_url(self, game_domain, mod_id, file_id):
            return None

    d = Downloader(NoUrlAPI())
    d.session = FakeSession(response=FakeResponse(b"x"))
    with pytest.raises(DownloadError) as excinfo:
        d.download_mod("skyrim", _mod(), tmp_path)
    assert "no download URL" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Service wiring: manifest MD5 attachment
# ---------------------------------------------------------------------------

def test_attach_expected_md5_only_matching_identity(tmp_path):
    from nexus_collection_dl.service import _attach_expected_md5
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.manifest_data = {
        "mod_rules": [
            {
                "source": {
                    "modId": 1, "fileID": 2,
                    "logicalFileName": "Mod-1.0.zip", "fileMD5": "ab" * 16,
                },
                "reference": {"type": "include", "logicalFileName": "Mod-1.0.zip"},
            },
        ],
    }
    mods_list = [
        {"mod_id": 1, "file_id": 2, "filename": "Mod-1.0.zip"},
        {"mod_id": 9, "file_id": 9, "filename": "Other.zip"},
    ]
    _attach_expected_md5(state, mods_list)
    assert mods_list[0]["expected_md5"] == "ab" * 16
    assert "expected_md5" not in mods_list[1]  # never attached across identities


# ---------------------------------------------------------------------------
# Bugcheck regressions: batch validation order and interrupt cleanup
# ---------------------------------------------------------------------------

def test_batch_invalid_filename_skipped_others_downloaded(tmp_path):
    """Batch downloads validate before slicing/logging: a malformed entry is
    skipped and never becomes a path; remaining mods still download."""
    payload = b"payload"
    d, api = _make_downloader(response=FakeResponse(content=payload))
    good = _mod("Good-1.0.zip", size=len(payload))
    bad = _mod("../../evil.zip")
    results = d.download_mods("skyrim", [bad, good], tmp_path)
    assert [m["filename"] for m, _ in results] == ["Good-1.0.zip"]
    assert (tmp_path / "Good-1.0.zip").read_bytes() == payload
    assert list(tmp_path.iterdir()) == [tmp_path / "Good-1.0.zip"]
    assert not (tmp_path.parent / "evil.zip").exists()
    assert api.download_url_calls == 1  # the bad entry never hit the API


def test_interrupt_cleans_temp_and_leaves_no_partial(tmp_path):
    """Ctrl+C mid-stream: the temp file is removed, no partial final file."""
    class InterruptingResponse(FakeResponse):
        def iter_content(self, chunk_size=8192):
            yield b"par"
            raise KeyboardInterrupt

    d, api = _make_downloader(response=InterruptingResponse(content=b"partial"))
    with pytest.raises(KeyboardInterrupt):
        d.download_mod("skyrim", _mod(), tmp_path)
    assert list(tmp_path.glob(".nexus-download-*")) == []
    assert not (tmp_path / "Mod-1.0.zip").exists()
