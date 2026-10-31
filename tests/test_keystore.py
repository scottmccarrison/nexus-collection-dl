"""Tests for the machine-local API key store (keystore.py).

Covers the 0600 atomic write, local (no-network) validation, removal
semantics that leave the rest of the config alone, the flag > env >
stored resolution precedence, and leak hygiene: a sentinel secret must
never surface in exception text, reprs, or captured log records. No
network, no host config: XDG_CONFIG_HOME is pinned to tmp_path.
"""

import stat
from pathlib import Path

import pytest

from nexus_collection_dl import keystore
from nexus_collection_dl.keystore import (
    KeyStoreError,
    api_key_path,
    load_api_key,
    remove_api_key,
    resolve_api_key,
    store_api_key,
    validate_api_key,
)

SECRET = "StoredKeyValue-abc123_XYZ789"
FLAG_KEY = "FlagKeyValue-abc123"
ENV_KEY = "EnvKeyValue-abc123"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Pin XDG config to tmp_path and clear any host NEXUS_API_KEY."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    return tmp_path


def key_file() -> Path:
    return api_key_path()


# ---------------------------------------------------------------------------
# Storage: 0600, atomic, replaceable
# ---------------------------------------------------------------------------

def test_store_writes_key_file_with_0600_permissions():
    path = store_api_key(SECRET)
    assert path == key_file()
    assert path.read_text() == SECRET + "\n"
    # fchmod is absolute, so this is exact regardless of umask.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A freshly created config directory carries no group/other bits.
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0


def test_store_replaces_existing_key_and_leaves_no_temp_files():
    store_api_key(SECRET)
    store_api_key(FLAG_KEY)
    assert load_api_key() == FLAG_KEY
    leftovers = [
        p for p in key_file().parent.iterdir()
        if p.name != keystore.API_KEY_FILENAME
    ]
    assert leftovers == []


def test_failed_write_preserves_old_key_and_cleans_temp(monkeypatch):
    """A mid-write failure is a KeyStoreError: old key intact, no temp."""
    store_api_key(SECRET)

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(keystore.os, "replace", boom)
    with pytest.raises(KeyStoreError) as excinfo:
        store_api_key(FLAG_KEY)
    # The failure message never carries the attempted value.
    assert FLAG_KEY not in str(excinfo.value)
    assert FLAG_KEY not in repr(excinfo.value)
    assert load_api_key() == SECRET  # old value intact
    temps = [
        p for p in key_file().parent.iterdir()
        if p.name.startswith(f".{keystore.API_KEY_FILENAME}.")
    ]
    assert temps == []


# ---------------------------------------------------------------------------
# Load and removal
# ---------------------------------------------------------------------------

def test_load_missing_or_empty_file_is_not_set():
    assert load_api_key() is None
    key_file().parent.mkdir(parents=True, exist_ok=True)
    key_file().write_text("\n  \n")
    assert load_api_key() is None


def test_unreadable_store_raises_without_value(monkeypatch):
    store_api_key(SECRET)

    def deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(KeyStoreError) as excinfo:
        load_api_key()
    message = str(excinfo.value)
    assert SECRET not in message
    assert SECRET not in repr(excinfo.value)
    assert str(key_file()) in message          # actionable: names the file
    assert "remove-api-key" in message         # actionable: names the fix


def test_remove_returns_state_and_preserves_other_config():
    store_api_key(SECRET)
    sibling = key_file().parent / "handler.json"
    sibling.write_text('{"version": 1, "mappings": {}}')

    assert remove_api_key() is True
    assert not key_file().exists()
    assert sibling.exists()                    # rest of config untouched
    assert load_api_key() is None

    assert remove_api_key() is False           # idempotent
    assert sibling.read_text() == '{"version": 1, "mappings": {}}'


# ---------------------------------------------------------------------------
# Validation (local only, never echoes the value)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "",
    "     ",
    "\n",
    "Ab12xy",                       # too short (< MIN_KEY_LENGTH)
    "Z" * 513,                      # too long (> MAX_KEY_LENGTH)
    "Spaced Value-abc123",          # interior space
    "Tab\tValue-abc123",            # control character
    "Nul\x00Value-abc123",          # NUL byte
    "Uni\u2713Value-abc123",        # non-ASCII
])
def test_validate_rejects_implausible_keys(bad):
    with pytest.raises(KeyStoreError):
        validate_api_key(bad)


@pytest.mark.parametrize("bad,fragment", [
    ("Ab12xy", "Ab12xy"),
    ("Z" * 513, "Z" * 30),
    ("Spaced Value-abc123", "Spaced Value"),
    ("Tab\tValue-abc123", "Value-abc123"),
    ("Uni\u2713Value-abc123", "Value-abc123"),
])
def test_validation_errors_never_echo_the_value(bad, fragment):
    with pytest.raises(KeyStoreError) as excinfo:
        validate_api_key(bad)
    assert fragment not in str(excinfo.value)
    assert fragment not in repr(excinfo.value)


def test_validate_strips_surrounding_whitespace():
    assert validate_api_key(f"  {SECRET} \n") == SECRET


def test_store_rejects_implausible_key_without_writing():
    with pytest.raises(KeyStoreError):
        store_api_key("Ab12xy")
    assert not key_file().exists()


# ---------------------------------------------------------------------------
# Resolution precedence: flag > env > stored > none
# ---------------------------------------------------------------------------

def test_precedence_flag_over_env_over_stored(monkeypatch):
    store_api_key(SECRET)
    monkeypatch.setenv("NEXUS_API_KEY", ENV_KEY)

    assert resolve_api_key(FLAG_KEY) == (FLAG_KEY, "flag")
    assert resolve_api_key(None) == (ENV_KEY, "env")

    monkeypatch.delenv("NEXUS_API_KEY")
    assert resolve_api_key(None) == (SECRET, "stored")

    assert remove_api_key() is True
    assert resolve_api_key(None) == (None, None)


def test_blank_flag_and_env_fall_through_to_stored(monkeypatch):
    store_api_key(SECRET)
    assert resolve_api_key("   ") == (SECRET, "stored")
    assert resolve_api_key("") == (SECRET, "stored")
    monkeypatch.setenv("NEXUS_API_KEY", "  ")
    assert resolve_api_key(None) == (SECRET, "stored")


def test_unreadable_store_raises_only_when_no_flag_or_env(monkeypatch):
    store_api_key(SECRET)

    def deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(KeyStoreError):
        resolve_api_key(None)
    # A flag or env value short-circuits before the broken store is read.
    assert resolve_api_key(FLAG_KEY) == (FLAG_KEY, "flag")
    monkeypatch.setenv("NEXUS_API_KEY", ENV_KEY)
    assert resolve_api_key(None) == (ENV_KEY, "env")


# ---------------------------------------------------------------------------
# Leak hygiene across the whole module surface
# ---------------------------------------------------------------------------

def test_no_secret_in_logs_across_operations(caplog, monkeypatch):
    with caplog.at_level(0):
        store_api_key(SECRET)
        load_api_key()
        resolve_api_key(FLAG_KEY)
        resolve_api_key(None)
        remove_api_key()
    assert SECRET not in caplog.text
    assert FLAG_KEY not in caplog.text
