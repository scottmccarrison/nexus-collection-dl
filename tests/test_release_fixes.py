"""Three release-blocking fixes from the adversarial review.

1. gc: only delete an archive when ALL contributed files are present
   (was: any - an interrupted extraction loses its only complete copy).
2. --game-dir/--prefix: reject nonexistent paths at the CLI.
3. NO_COLOR: honor the environment variable.
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# 1. gc completeness check
# ---------------------------------------------------------------------------


def test_gc_keeps_archive_when_extraction_is_incomplete(tmp_path):
    """An interrupted extraction means the archive is the only complete copy."""
    from nexus_collection_dl.gc import plan_gc
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")

    # Mod with 3 contributed files, but only 1 is on disk (partial extraction)
    state.add_mod({
        "mod_id": 1, "mod_name": "M", "file_id": 1,
        "filename": "m.zip", "version": "1", "phase": 0,
    })
    state.mods[1].contributed_files = ["Data/a.esp", "Data/b.esp", "Data/c.esp"]
    state.save()

    # Create the archive and one of three contributed files
    archive = mods / "m.zip"
    archive.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    (mods / "Data").mkdir()
    (mods / "Data" / "a.esp").write_text("x")
    # b.esp and c.esp are MISSING (interrupted extraction)

    plan = plan_gc(mods)
    kept_names = [c[0].name for c in plan.kept]
    reclaimable_names = [c.name for c in plan.reclaimable]
    assert "m.zip" in kept_names, (
        f"archive must be kept when extraction is incomplete: kept={kept_names}"
    )
    assert "m.zip" not in reclaimable_names, (
        f"archive must not be reclaimable: {reclaimable_names}"
    )


def test_gc_reclaims_archive_when_extraction_is_complete(tmp_path):
    """When all contributed files are present, the archive is reclaimable."""
    from nexus_collection_dl.gc import plan_gc
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod({
        "mod_id": 1, "mod_name": "M", "file_id": 1,
        "filename": "m.zip", "version": "1", "phase": 0,
    })
    state.mods[1].contributed_files = ["Data/a.esp"]
    state.save()

    archive = mods / "m.zip"
    archive.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    (mods / "Data").mkdir()
    (mods / "Data" / "a.esp").write_text("x")

    plan = plan_gc(mods)
    reclaimable_names = [c.name for c in plan.reclaimable]
    assert "m.zip" in reclaimable_names, (
        f"complete extraction should be reclaimable: {reclaimable_names}"
    )


# ---------------------------------------------------------------------------
# 2. --game-dir / --prefix CLI validation
# ---------------------------------------------------------------------------


def test_deploy_rejects_nonexistent_game_dir_at_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n",
        "collection_revision": 1, "game_domain": "skyrimspecialedition",
        "mods": {}, "tombstones": {},
    }))
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module
    r = CliRunner().invoke(
        cli_module.main,
        ["deploy", str(mods), "--game-dir", "/nonexistent-xyz"],
    )
    assert r.exit_code != 0
    assert "Traceback" not in (r.output + str(r.exception or ""))


# ---------------------------------------------------------------------------
# 3. NO_COLOR
# ---------------------------------------------------------------------------


def test_no_color_disables_ansi_in_shell_prompt(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    from nexus_collection_dl.shell import _ShellSession
    # Re-read the prompt after setting NO_COLOR
    s = _ShellSession.__new__(_ShellSession)
    s.current = None
    prompt = s._prompt()
    assert "\x1b" not in prompt, f"NO_COLOR must disable ANSI: {prompt!r}"
