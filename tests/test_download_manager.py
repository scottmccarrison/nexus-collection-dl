"""Download-manager behaviour: archives survive extraction and gc reclaims.

Every mod manager keeps the downloaded file and lets the user clean up
later. Here extraction used to delete the archive, which meant gc (a
disk-reclaim tool) had nothing to reclaim: the everyday path threw away
its own input. Extraction keeps the archive, gc reclaims the ones whose
payload is on disk, and the tool points out the reclaimable disk while
the user is working.

The visibility nudge is one line after work commands. The user owns
whether to run gc; the tool owns making sure the disk is visible.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl import service as service_module
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    path.write_bytes(_zip_bytes(entries))
    return path


# -- the everyday loop: extract keeps, gc reclaims -----------------------


def test_sync_then_gc_reclaims_the_downloaded_archives(tmp_path, monkeypatch):
    """The whole point of gc: sync leaves the archive, gc buys it back.

    Before, sync deleted the archive and gc had nothing to do.
    """
    from nexus_collection_dl.gc import apply_gc, plan_gc
    from tests.test_service_regressions import FakeNexusAPI
    from tests.test_service_tombstones import (
        collection_data,
        manifest_entry,
        reload_state,
    )

    cdir = tmp_path / "Test Collection"

    def fake_download_mods(self, game_domain, mods, target_dir,
                           on_complete=None, on_progress=None,
                           show_progress=True):
        out = []
        for m in mods:
            p = Path(target_dir) / m["filename"]
            _make_zip(p, {"Data/thing.esp": b"x" * 100})
            out.append((m, p))
        return out

    monkeypatch.setattr(
        service_module.Downloader, "download_mods", fake_download_mods
    )
    svc = ModManagerService(api_key="test-key-unused")
    svc._api = FakeNexusAPI(
        is_premium=True,
        collection=collection_data([
            manifest_entry(123, 999, "Test Mod"),
        ]),
    )

    svc.sync(
        "https://next.nexusmods.com/skyrimspecialedition/collections/t",
        tmp_path, no_load_order=True,
    )

    state = reload_state(cdir)
    assert state.mods[999].contributed_files == ["Data/thing.esp"]
    # The archive survived extraction.
    assert list(cdir.glob("*.zip")), "sync deleted the archive"

    plan = plan_gc(cdir)
    assert plan.reclaimable, plan
    assert plan.bytes_reclaimable > 0

    freed = apply_gc(plan)
    assert freed > 0
    after = plan_gc(cdir)
    assert after.reclaimable == [], after
    # The payload is what was deployed; it stays.
    assert (cdir / "Data" / "thing.esp").is_file()


def test_gc_keeps_an_archive_that_is_the_only_copy(tmp_path):
    """A mod that has not been extracted still needs its archive."""
    from nexus_collection_dl.gc import plan_gc

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Body", "file_id": 11,
         "filename": "body.zip", "version": "1"}
    )
    state.mods[11].contributed_files = ["body.zip"]
    state.save()
    (mods / "body.zip").write_bytes(_zip_bytes({"a/one.esp": b"p"}))

    plan = plan_gc(mods)
    assert plan.reclaimable == []
    assert (mods / "body.zip").exists()


def test_kept_archives_are_not_deployed(tmp_path):
    """Keeping the archive must not ship it into the game directory.

    The archive is a download container, not game content. Before this
    was pinned, keeping archives made every one of them deployable.
    """
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_zip(mods / "Foo-1234-1-0.zip", {"a/one.esp": b"1"})
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1234, "mod_name": "Foo", "file_id": 10,
         "filename": "Foo-1234-1-0.zip", "version": "1.0",
         "download_status": "pending_download"}
    )
    state.save()

    svc = ModManagerService(api_key="test-key-unused")
    result = svc.import_downloads(mods, no_load_order=True)
    assert result.extracted == 1, result.errors
    # The archive survived extraction (download-manager behaviour).
    assert (mods / "Foo-1234-1-0.zip").exists()

    game = tmp_path / "game"
    game.mkdir()
    deploy = svc.deploy(mods, game_dir=game, use_copy=True)
    assert deploy.errors == [], deploy
    # The payload landed; the container did not.
    assert (game / "Data" / "a" / "one.esp").is_file()
    assert not list(game.rglob("*.zip"))


# -- the visibility nudge ------------------------------------------------


def test_nudge_message_reports_reclaimable_disk(tmp_path):
    from nexus_collection_dl.gc import nudge_message

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Body", "file_id": 11,
         "filename": "body.zip", "version": "1"}
    )
    state.mods[11].contributed_files = ["a/one.esp"]
    (mods / "a").mkdir()
    (mods / "a" / "one.esp").write_bytes(b"p")
    state.save()
    (mods / "body.zip").write_bytes(_zip_bytes({"a/one.esp": b"x" * 5000}))

    msg = nudge_message(mods, threshold_bytes=100)
    assert msg is not None
    assert "reclaim" in msg.lower()
    assert "gc" in msg


def test_nudge_message_is_silent_below_the_threshold(tmp_path):
    from nexus_collection_dl.gc import nudge_message

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Body", "file_id": 11,
         "filename": "body.zip", "version": "1"}
    )
    state.mods[11].contributed_files = ["a/one.esp"]
    (mods / "a").mkdir()
    (mods / "a" / "one.esp").write_bytes(b"p")
    state.save()
    (mods / "body.zip").write_bytes(_zip_bytes({"a/one.esp": b"x"}))

    assert nudge_message(mods, threshold_bytes=10 * 1024 * 1024) is None


def test_nudge_message_is_silent_when_nothing_is_reclaimable(tmp_path):
    """No archives, or every archive is a only copy: nothing to say."""
    from nexus_collection_dl.gc import nudge_message

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.save()
    assert nudge_message(mods, threshold_bytes=1) is None


# -- gc messaging -------------------------------------------------------


def test_gc_says_why_when_there_is_nothing_to_reclaim(tmp_path):
    """A quiet run must not be a dead end. Say what was looked for."""
    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.save()

    r = CliRunner().invoke(cli_module.main, ["gc", str(mods)])
    assert r.exit_code == 0, r.output
    assert "Nothing to report" not in r.output
    assert "reclaim" in r.output.lower()


# -- wiring: the nudge reaches the human, not the json -------------------


def _collection_with_reclaimable_archive(tmp_path: Path) -> Path:
    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Body", "file_id": 11,
         "filename": "body.zip", "version": "1"}
    )
    state.mods[11].contributed_files = ["a/one.esp"]
    (mods / "a").mkdir()
    (mods / "a" / "one.esp").write_bytes(b"p")
    state.save()
    (mods / "body.zip").write_bytes(_zip_bytes({"a/one.esp": b"x" * 5000}))
    return mods


def _fake_add_service():
    from nexus_collection_dl.service import AddResult

    class FakeSvc:
        def __init__(self, api_key=None, force_free=False):
            pass

        def add_mod(self, *a, **kw):
            return AddResult(
                mod_name="Body", mod_id=1, file_name="body.zip", success=True
            )

    return FakeSvc


def test_cli_prints_the_nudge_after_a_work_command(tmp_path, monkeypatch):
    """A work command points at the reclaimable disk without being asked.

    The gc command reports on gc; this is the tool noticing while the
    user is doing something else entirely.
    """
    mods = _collection_with_reclaimable_archive(tmp_path)
    import nexus_collection_dl.gc as gc_module
    monkeypatch.setattr(gc_module, "NUDGE_THRESHOLD_BYTES", 100)
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)
    monkeypatch.setattr(cli_module, "ModManagerService", _fake_add_service())

    r = CliRunner().invoke(
        cli_module.main,
        ["--api-key", "k", "add",
         "https://www.nexusmods.com/skyrimspecialedition/mods/1", str(mods)],
    )
    assert r.exit_code == 0, r.output
    assert "reclaim" in r.output.lower()
    assert "gc" in r.output


def test_cli_nudge_does_not_pollute_json(tmp_path, monkeypatch):
    """--json output must stay parseable. The nudge is for humans."""
    mods = _collection_with_reclaimable_archive(tmp_path)
    import nexus_collection_dl.gc as gc_module
    monkeypatch.setattr(gc_module, "NUDGE_THRESHOLD_BYTES", 100)
    monkeypatch.setattr(cli_module, "check_for_update", lambda: None)
    monkeypatch.setattr(cli_module, "ModManagerService", _fake_add_service())

    r = CliRunner().invoke(
        cli_module.main,
        ["--api-key", "k", "--json", "add",
         "https://www.nexusmods.com/skyrimspecialedition/mods/1", str(mods)],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["ok"] is True
    # No prose leaked into the envelope.
    assert "reclaim" not in r.output.lower()
