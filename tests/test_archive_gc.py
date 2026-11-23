"""#135: `nexus-dl gc` reclaims disk from archives that are done.

Downloaded archives pile up. The default flow deletes the extracted
archive after publish, but `--no-extract` retains it, and re-runs and
multiple collections accumulate. The only recourse is manual deletion,
which is easy to get wrong: deleting something the backups or a future
undeploy still needs.

Dry-run by default. It reports what is reclaimable and touches nothing
until `--apply`. It never touches `.nexus-backups/`, and it never
touches an archive that is the only copy of an unverified or
partially-extracted mod.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.state import CollectionState


def _state_with(tmp_path: Path, **fields) -> CollectionState:
    mods = tmp_path / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {
            "mod_id": fields.get("mod_id", 1),
            "mod_name": fields.get("name", "Body"),
            "file_id": fields.get("file_id", 11),
            "filename": fields.get("filename", "body.zip"),
            "version": "1",
        }
    )
    # Attribution recorded at extract time. The mod has payload on disk,
    # so its archive is redundant.
    payload = fields.get("contributed_files", ["a/one.esp"])
    state.mods[fields.get("file_id", 11)].contributed_files = list(payload)
    for f in payload:
        path = mods / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"p")
    return state


def _archive(mods: Path, name: str, payload: bytes = b"x" * 100) -> Path:
    path = mods / name
    path.write_bytes(payload)
    return path


def test_gc_reports_reclaimable_archives(tmp_path):
    """Dry-run first: what would go, and how much."""
    from nexus_collection_dl.gc import plan_gc

    state = _state_with(tmp_path)
    state.save()
    _archive(state.mods_dir, "body.zip")

    plan = plan_gc(state.mods_dir)
    assert plan.reclaimable, plan
    assert plan.reclaimable[0].name == "body.zip"
    assert plan.bytes_reclaimable == 100


def test_gc_by_default_touches_nothing(tmp_path):
    """Dry-run is the default. `--apply` is how anything gets deleted."""
    state = _state_with(tmp_path)
    state.save()
    _archive(state.mods_dir, "body.zip")

    r = CliRunner().invoke(cli_module.main, ["gc", str(state.mods_dir)])
    assert r.exit_code == 0, r.output
    assert (state.mods_dir / "body.zip").exists()


def test_gc_apply_deletes_reclaimable_archives(tmp_path):
    state = _state_with(tmp_path)
    state.save()
    _archive(state.mods_dir, "body.zip")

    r = CliRunner().invoke(cli_module.main, ["gc", str(state.mods_dir), "--apply"])
    assert r.exit_code == 0, r.output
    assert not (state.mods_dir / "body.zip").exists()


def test_gc_never_touches_backups(tmp_path):
    """The backup store is what undeploy restores from. Never delete it."""
    from nexus_collection_dl.gc import plan_gc

    state = _state_with(tmp_path)
    state.save()
    backups = state.mods_dir / ".nexus-backups"
    backups.mkdir()
    _archive(backups, "deadbeef")

    plan = plan_gc(state.mods_dir)
    assert plan.reclaimable == []


def test_gc_keeps_the_only_copy_of_an_unextracted_mod(tmp_path):
    """An archive with no extracted payload is the only copy. Keep it.

    Deleting it would lose the mod.
    """
    from nexus_collection_dl.gc import plan_gc

    mods = tmp_path / "mods"
    mods.mkdir()
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Body", "file_id": 11, "filename": "body.zip", "version": "1"}
    )
    state.save()
    _archive(mods, "body.zip")

    plan = plan_gc(mods)
    assert plan.reclaimable == []


def test_gc_keeps_archives_the_state_does_not_know(tmp_path):
    """A file nobody claims is not ours to delete."""
    from nexus_collection_dl.gc import plan_gc

    state = _state_with(tmp_path)
    state.save()
    _archive(state.mods_dir, "body.zip")
    _archive(state.mods_dir, "stranger.zip")

    plan = plan_gc(state.mods_dir)
    names = {p.name for p in plan.reclaimable}
    assert "stranger.zip" not in names


def test_gc_reports_the_bytes(tmp_path):
    """The number is the point: how much disk does this buy back."""
    from nexus_collection_dl.gc import plan_gc

    state = _state_with(tmp_path)
    state.save()
    _archive(state.mods_dir, "body.zip", b"x" * 4096)

    plan = plan_gc(state.mods_dir)
    assert plan.bytes_reclaimable == 4096
