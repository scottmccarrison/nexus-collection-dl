"""CliRunner tests for --json output.

Boundary-level: real click invocation of `nexus-dl --json`, with a fake
service. Verifies the envelope is stable, stdout is pure JSON, errors carry
machine-readable codes, and the flag is refused where it cannot apply.

The schema assertions pin field names on purpose: a rename that breaks a
consumer should break a test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl import updater as updater_module
from nexus_collection_dl.api import NexusAPIError, NexusPremiumRequired
from nexus_collection_dl.service import (
    AddResult,
    RemoveModResult,
    StatusResult,
    SyncResult,
)
from nexus_collection_dl.state import StateBusyError, StateError

# --- fakes -----------------------------------------------------------------


class FakeService:
    """Stand-in returned by the patched ModManagerService factory."""

    status_result: ClassVar[Any] = None
    sync_result: ClassVar[Any] = None
    remove_result: ClassVar[Any] = None
    error: ClassVar[BaseException | None] = None
    calls: ClassVar[list] = []

    def __init__(self, api_key=None, force_free=False):
        FakeService.calls.append({"api_key": api_key})

    def get_status(self, mods_dir):
        if FakeService.error:
            raise FakeService.error
        return FakeService.status_result or StatusResult(
            collection_name="c", collection_url="u", game_domain="skyrimspecialedition",
            installed_revision=1, latest_revision=1, mods=[],
            deployed_at=None, deployed_file_count=0, game_dir="",
            track_sync_enabled=False,
        )

    def sync(self, *a, **kw):
        if FakeService.error:
            raise FakeService.error
        return FakeService.sync_result or SyncResult(
            mods_downloaded=0, mods_extracted=0, errors=[],
            load_order_files=[], tracked=0, untracked=0,
            pending_downloads=[], collection_dir=Path("/tmp/c"),
            conflicts=[], removed_skipped=[],
        )

    def remove_mod(self, mods_dir, **kw):
        if FakeService.error:
            raise FakeService.error
        return FakeService.remove_result or RemoveModResult(
            removed_file_ids=[1], removed_mod_ids=[2], warnings=[]
        )

    def add_mod(self, *a, **kw):
        if FakeService.error:
            raise FakeService.error
        return AddResult(
            mod_name="M", mod_id=1, file_name="m.zip",
            success=False, error="nope",
        )


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    FakeService.status_result = None
    FakeService.sync_result = None
    FakeService.remove_result = None
    FakeService.error = None
    FakeService.calls = []
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)
    monkeypatch.setattr(updater_module, "check_and_prompt_update", lambda c: None)
    monkeypatch.setattr(cli_module, "ModManagerService", FakeService)
    monkeypatch.setattr(cli_module.console, "width", 400)
    monkeypatch.chdir(tmp_path)


def invoke(args: list[str]):
    # --json must precede the subcommand, as the docs say.
    return CliRunner().invoke(cli_module.main, ["--api-key", "k", "--json"] + args)


def stdout_of(result) -> str:
    """True stdout.

    click 8.5's CliRunner mixes stderr into ``result.output``, so subtract
    the exact ``result.stderr`` text to recover what a piped consumer sees.
    """
    out = result.output
    err = result.stderr or ""
    return out.replace(err, "", 1) if err else out


def payload(result) -> dict:
    """Parse stdout as JSON. Fails loudly if anything else is in there."""
    return json.loads(stdout_of(result))


# --- envelope shape --------------------------------------------------------


def test_envelope_has_exactly_three_keys():
    r = invoke(["collections"])
    assert set(payload(r)) == {"ok", "result", "error"}


def test_success_sets_ok_true_and_null_error(tmp_path):
    r = invoke(["collections", str(tmp_path)])
    d = payload(r)
    assert d["ok"] is True
    assert d["error"] is None
    assert r.exit_code == 0


def test_failure_sets_ok_false_and_null_result(tmp_path):
    FakeService.error = StateError("no state")
    r = invoke(["status", str(tmp_path)])
    d = payload(r)
    assert d["ok"] is False
    assert d["result"] is None
    assert r.exit_code == 1


def test_result_is_always_an_object_never_a_bare_list(tmp_path):
    r = invoke(["collections", str(tmp_path)])
    assert isinstance(payload(r)["result"], dict)


# --- field names are pinned so renames break tests -------------------------


def test_collections_result_shape(tmp_path):
    r = invoke(["collections", str(tmp_path)])
    d = payload(r)
    assert set(d["result"]) == {"root", "collections"}
    assert isinstance(d["result"]["root"], str)


def test_games_result_shape(tmp_path):
    r = invoke(["games", str(tmp_path)])
    assert set(payload(r)["result"]) == {"root", "games"}


def test_health_result_shape(tmp_path):
    r = invoke(["health", str(tmp_path)])
    assert set(payload(r)["result"]) == {"root", "checks"}


def test_status_result_is_the_dataclass_fields():
    r = invoke(["status", "/tmp"])
    assert set(payload(r)["result"]) == {
        "collection_name", "collection_url", "game_domain",
        "installed_revision", "latest_revision", "mods",
        "deployed_at", "deployed_file_count", "game_dir",
        "track_sync_enabled", "removed_skipped",
    }


def test_remove_result_shape(tmp_path):
    r = invoke(["remove", str(tmp_path), "--file-id", "1"])
    assert set(payload(r)["result"]) == {
        "removed_file_ids", "removed_mod_ids", "warnings",
    }


# --- errors ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_type"),
    [
        (StateError("x"), "state_error", "StateError"),
        (StateBusyError("x"), "state_busy", "StateBusyError"),
        (NexusAPIError("x"), "nexus_api_error", "NexusAPIError"),
        (NexusPremiumRequired("x"), "nexus_premium_required", "NexusPremiumRequired"),
        (ValueError("x"), "invalid_argument", "ValueError"),
    ],
)
def test_error_code_mapping(exc, expected_code, expected_type):
    """Subclasses must win over their parents: StateBusyError is a StateError."""
    from nexus_collection_dl.jsonout import error_code_for

    assert error_code_for(exc) == expected_code
    assert type(exc).__name__ == expected_type


def test_error_envelope_for_a_caught_exception(tmp_path):
    """Integration: the envelope a command actually emits."""
    FakeService.error = StateError("x")
    r = invoke(["status", str(tmp_path)])
    d = payload(r)
    assert d["ok"] is False
    assert d["error"]["code"] == "state_error"
    assert d["error"]["type"] == "StateError"
    assert d["error"]["message"] == "x"
    assert r.exit_code == 1


def test_error_carries_hint_when_there_is_one(tmp_path):
    FakeService.error = StateError("x")
    r = invoke(["status", str(tmp_path)])
    assert payload(r)["error"]["hint"] == "Run 'sync' first to download a collection."


def test_unmapped_exception_falls_back_to_class_name():
    from nexus_collection_dl.jsonout import error_code_for

    assert error_code_for(RuntimeError("x")) == "RuntimeError"


def test_partial_failure_keeps_the_result(tmp_path):
    """AddResult.success False: ok is false but the caller still gets the result."""
    r = invoke(["add", "https://nexusmods.com/skyrimspecialedition/mods/1", str(tmp_path)])
    d = payload(r)
    assert d["ok"] is False
    assert d["result"] is not None
    assert d["error"]["code"] == "operation_failed"
    assert r.exit_code == 1


# --- stdout purity ---------------------------------------------------------


def test_stdout_is_pure_json(tmp_path):
    r = invoke(["collections", str(tmp_path)])
    out = stdout_of(r)
    json.loads(out)          # no banner, no prose
    assert "\x1b" not in out  # no ANSI
    assert "nexus-dl v" not in out


def test_paths_are_strings_not_path_reprs(tmp_path):
    r = invoke(["collections", str(tmp_path)])
    d = payload(r)
    assert isinstance(d["result"]["root"], str)
    assert "PosixPath" not in stdout_of(r)


def test_json_mode_emits_no_human_error_line(tmp_path):
    """In JSON mode the envelope is the whole story: no red prose anywhere.

    The diagnostic that used to print is replaced by error.message in the
    payload, so a piped consumer never has to strip human text.
    """
    FakeService.error = StateError("x")
    r = invoke(["status", str(tmp_path)])
    out = stdout_of(r)
    json.loads(out)                       # payload parses
    assert "Error:" not in out
    assert "Error:" not in (r.stderr or "")
    assert payload(r)["error"]["message"] == "x"


def test_human_mode_still_prints_the_red_line(tmp_path):
    """The non-JSON path is unchanged: red error plus hint on stdout."""
    FakeService.error = StateError("x")
    r = CliRunner().invoke(cli_module.main, ["--api-key", "k", "status", str(tmp_path)])
    assert r.exit_code == 1
    assert "Error:" in r.output
    assert "Run 'sync' first" in r.output


# --- the flag is refused where it cannot apply ------------------------------


@pytest.mark.parametrize("cmd", ["shell", "serve", "handler", "nxm"])
def test_json_refused_for_interactive_commands(cmd):
    r = CliRunner().invoke(cli_module.main, ["--json", cmd])
    assert r.exit_code == 2
    # a usage error, not a JSON envelope
    with pytest.raises(json.JSONDecodeError):
        json.loads(stdout_of(r) or "")
    assert "--json is not supported" in r.stderr
