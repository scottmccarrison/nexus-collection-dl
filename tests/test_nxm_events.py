"""Tests for the per-collection nxm click-event file.

The event file is the channel a running `nexus-dl shell` uses to learn that
a browser click landed in a collection it has open. These cover the write
half: the line format, the append mechanics, best-effort failure, and that
the nxm command records the right outcome for each result shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_collection_dl.handler import (
    EVENTS_FILENAME,
    append_nxm_event,
    events_path,
)


def read_events(mods_dir: Path) -> list[dict]:
    path = events_path(mods_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- append mechanics ------------------------------------------------------


def test_appends_one_json_line(tmp_path):
    assert append_nxm_event(
        tmp_path,
        {
            "outcome": "downloaded",
            "collection_name": "Clean Visuals",
            "mod_id": 123,
            "file_id": 456,
            "mod_name": "SkyUI",
            "filename": "SkyUI.7z",
            "extracted": True,
        },
    ) is True
    events = read_events(tmp_path)
    assert len(events) == 1
    e = events[0]
    assert e["v"] == 1
    assert e["outcome"] == "downloaded"
    assert e["collection_name"] == "Clean Visuals"
    assert e["mod_id"] == 123
    assert e["file_id"] == 456
    assert e["mod_name"] == "SkyUI"
    assert e["extracted"] is True
    assert e["reason"] == ""
    assert e["ts"].endswith("Z")


def test_second_append_adds_a_line(tmp_path):
    append_nxm_event(tmp_path, {"outcome": "downloaded", "mod_name": "A"})
    append_nxm_event(tmp_path, {"outcome": "already_downloaded", "mod_name": "B"})
    assert [e["mod_name"] for e in read_events(tmp_path)] == ["A", "B"]


def test_unknown_outcome_is_recorded_as_error(tmp_path):
    append_nxm_event(tmp_path, {"outcome": "invented_by_a_hacker"})
    assert read_events(tmp_path)[0]["outcome"] == "error"


def test_missing_ids_become_zero_not_null(tmp_path):
    append_nxm_event(tmp_path, {"outcome": "error", "mod_id": "not-a-number"})
    e = read_events(tmp_path)[0]
    assert e["mod_id"] == 0
    assert e["file_id"] == 0


def test_file_is_private(tmp_path):
    append_nxm_event(tmp_path, {"outcome": "downloaded"})
    assert (tmp_path / EVENTS_FILENAME).stat().st_mode & 0o777 == 0o600


def test_failure_returns_false_and_writes_nothing(tmp_path):
    # A path that cannot be created must not raise.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    assert append_nxm_event(blocker, {"outcome": "downloaded"}) is False
    assert not (blocker / EVENTS_FILENAME).exists()


def test_events_path_is_next_to_state(tmp_path):
    assert events_path(tmp_path).name == EVENTS_FILENAME


# --- rotation --------------------------------------------------------------


def test_rotation_keeps_only_the_tail_at_the_cap(tmp_path, monkeypatch):
    from nexus_collection_dl import handler as handler_module

    monkeypatch.setattr(handler_module, "_EVENTS_MAX_BYTES", 64)
    for i in range(6):
        append_nxm_event(tmp_path, {"outcome": "downloaded", "mod_name": f"m{i}"})

    assert events_path(tmp_path).exists()
    # Rotated aside at least once; the live file holds only recent lines.
    assert (tmp_path / (EVENTS_FILENAME + ".1")).exists()
    assert len(read_events(tmp_path)) < 6


def test_rotation_is_skipped_when_the_lock_is_busy(tmp_path, monkeypatch):
    """Losing a rotation is harmless; losing an event is not."""
    from nexus_collection_dl import handler as handler_module

    monkeypatch.setattr(handler_module, "_EVENTS_MAX_BYTES", 1)

    class BusyLock:
        def __init__(self, *a, **kw):
            raise RuntimeError("busy")

    monkeypatch.setattr("nexus_collection_dl.state.CollectionLock", BusyLock)
    for i in range(3):
        assert append_nxm_event(tmp_path, {"outcome": "downloaded", "mod_name": f"m{i}"}) is True
    assert len(read_events(tmp_path)) == 3
    assert not (tmp_path / (EVENTS_FILENAME + ".1")).exists()


# --- the nxm command records the right outcome ----------------------------
# These drive the real click path with a fake service, so the event file is
# the observable.


class _FakeService:
    result = None
    error = None

    def __init__(self, api_key=None, force_free=False):
        pass

    def add_nxm(self, uri, mods_dir=None, output_dir=None, **kw):
        if _FakeService.error:
            raise _FakeService.error
        return _FakeService.result


@pytest.fixture()
def run_nxm(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module
    from nexus_collection_dl import updater as updater_module

    _FakeService.result = None
    _FakeService.error = None
    monkeypatch.setattr(cli_module, "ModManagerService", _FakeService)
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)
    monkeypatch.setattr(updater_module, "check_and_prompt_update", lambda c: None)
    monkeypatch.setattr(cli_module, "handler_notify", lambda *a, **kw: None)
    mods = tmp_path / "coll"
    mods.mkdir()

    def run(**result_kwargs):
        from nexus_collection_dl.service import NxmResult

        defaults = {
            "success": True,
            "status": "downloaded",
            "game_domain": "skyrimspecialedition",
            "mod_id": 1,
            "file_id": 2,
            "mod_name": "SkyUI",
            "filename": "SkyUI.7z",
            "collection_dir": mods,
            "extracted": True,
        }
        defaults.update(result_kwargs)
        _FakeService.result = NxmResult(**defaults)
        return CliRunner().invoke(
            cli_module.main,
            ["--api-key", "k", "nxm",
             "nxm://skyrimspecialedition/mods/1/files/2",
             "--mods-dir", str(mods), "--no-notify"],
        )

    return run, mods


def test_success_writes_a_downloaded_event(run_nxm):
    run, mods = run_nxm
    r = run()
    assert r.exit_code == 0
    events = read_events(mods)
    assert len(events) == 1
    assert events[0]["outcome"] == "downloaded"
    assert events[0]["mod_name"] == "SkyUI"


def test_already_downloaded_is_its_own_outcome(run_nxm):
    run, mods = run_nxm
    run(status="already_downloaded", extracted=False)
    assert read_events(mods)[0]["outcome"] == "already_downloaded"


def test_extracted_from_archive_is_its_own_outcome(run_nxm):
    run, mods = run_nxm
    run(status="extracted_from_archive")
    assert read_events(mods)[0]["outcome"] == "extracted_from_archive"


def test_saved_to_output_writes_no_event(run_nxm):
    """A standalone --output drop changed no collection."""
    run, mods = run_nxm
    run(status="saved_to_output", collection_dir=None,
        output_path=Path("/tmp/x.7z"), extracted=False)
    assert read_events(mods) == []


def test_pending_download_is_distinguished_from_error(run_nxm):
    run, mods = run_nxm
    run(success=False, status="error", download_status="pending_download",
        error="extraction failed")
    assert read_events(mods)[0]["outcome"] == "pending_download"


def test_plain_failure_writes_an_error_event(run_nxm):
    run, mods = run_nxm
    run(success=False, status="error", error="nope")
    e = read_events(mods)[0]
    assert e["outcome"] == "error"
    assert e["reason"] in ("", "rejected", "re_click_required")


def test_event_failure_does_not_change_the_exit_code(run_nxm, monkeypatch):
    from nexus_collection_dl import cli as cli_module

    run, _ = run_nxm
    monkeypatch.setattr(
        cli_module, "append_nxm_event",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    r = run()
    assert r.exit_code == 0
