"""Safety tests for tracked deployment, undeploy, and crash recovery.

These exercise real files on disk: real symlinks, real backups, real
journals, and real interruption windows (journal replay without mocks where
possible, fault injection where an interruption must be simulated). No
network access is involved.
"""

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from nexus_collection_dl import deploy as deploy_module
from nexus_collection_dl.deploy import (
    BackupStore,
    DeployError,
    DeployJournal,
    DeployedFile,
    DeploymentPlan,
    UndeployError,
    deploy as run_deploy,
    fingerprint_state,
    get_game_ini_path,
    get_plugins_txt_dest,
    recover_ownership,
    sha256_file,
    undeploy_records,
)
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionLock, CollectionState, StateBusyError, StateError

DOMAIN = "skyrimspecialedition"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class Env:
    pass


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    e = Env()
    e.tmp = tmp_path
    e.mods = tmp_path / "mods"
    e.mods.mkdir()
    e.game = tmp_path / "game"
    e.game.mkdir()
    e.staging = e.mods / "staging"
    e.staging.mkdir()
    e.backups = BackupStore(e.mods)
    e.journal = DeployJournal(e.mods)
    e.ownership: dict[str, dict] = {}
    return e


def _src(env: Env, name: str, content: bytes = b"mod content") -> Path:
    p = env.staging / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _deploy(env: Env, data_files, root_files=(), method="symlink"):
    plan = DeploymentPlan(
        game_root_files=[(Path(s), Path(r)) for s, r in root_files],
        data_files=[(Path(s), Path(r)) for s, r in data_files],
    )
    return run_deploy(
        plan, env.game, method=method, dry_run=False,
        backups=env.backups, journal=env.journal, ownership=env.ownership,
    )


def _undeploy(env: Env):
    return undeploy_records(env.ownership, [env.game], env.backups, env.journal)


@pytest.fixture()
def svc() -> ModManagerService:
    # deploy/undeploy never touch the network
    return ModManagerService(api_key="test-key-unused")


def _svc_env(tmp_path: Path, staging_files: dict[str, bytes] | None = None):
    """Service-level layout: mods dir with state, game dir, proton prefix."""
    mods = tmp_path / "mods"
    mods.mkdir()
    game = tmp_path / "game"
    game.mkdir()
    prefix = tmp_path / "pfx"
    prefix.mkdir()
    for name, content in (staging_files or {"ModA/plugin.esp": b"esp bytes"}).items():
        p = mods / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    state = CollectionState(mods)
    state.set_collection_info(
        "https://next.nexusmods.com/skyrimspecialedition/collections/test",
        "Test Collection", 1, DOMAIN,
    )
    state.game_dir = str(game)
    state.proton_prefix = str(prefix)
    state.save()
    return mods, game, prefix


def _manual_record(env: Env, src: Path, dest: Path, original: bytes | None) -> dict:
    """Build a valid current-format ownership record by hand (journal replay)."""
    rec = {
        "src": str(src),
        "dest": str(dest),
        "method": "copy",
        "kind": "mod",
        "sha256": sha256_file(src) if src.exists() else hashlib.sha256(b"").hexdigest(),
        "link_target": None,
        "had_original": original is not None,
        "orig_sha256": None,
        "orig_link_target": None,
        "orig_mode": 0o644,
        "backup": None,
        "ts": "2026-10-29T00:00:00+00:00",
    }
    if original is not None:
        # Store the original through the real backup store
        holder = env.tmp / f"orig-{dest.name}"
        holder.write_bytes(original)
        name, mode = env.backups.store_file(holder)
        rec["orig_sha256"] = name
        rec["backup"] = name
        rec["orig_mode"] = mode
        holder.unlink()
    return rec


# ---------------------------------------------------------------------------
# Basic deploy / undeploy cycles
# ---------------------------------------------------------------------------

def test_symlink_deploy_and_undeploy(env: Env):
    src = _src(env, "mod.esp")
    result = _deploy(env, [(src, Path("mod.esp"))])
    dest = env.game / "Data" / "mod.esp"
    assert result.deployed and not result.errors
    assert dest.is_symlink()
    assert os.readlink(dest) == str(src.resolve())

    kept, problems, notes, cleared = _undeploy(env)
    assert kept == [] and cleared == 1
    assert not dest.exists()
    assert src.exists()  # staging source is never touched


def test_copy_deploy_and_undeploy(env: Env):
    src = _src(env, "mod.esp", b"copy me")
    result = _deploy(env, [(src, Path("mod.esp"))], method="copy")
    dest = env.game / "Data" / "mod.esp"
    assert dest.read_bytes() == b"copy me"
    rec = env.ownership[str(dest)]
    assert rec["sha256"] == hashlib.sha256(b"copy me").hexdigest()
    assert rec["had_original"] is False

    kept, problems, notes, cleared = _undeploy(env)
    assert kept == [] and cleared == 1
    assert not dest.exists()


def test_preexisting_regular_original_restored_with_mode(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    os.chmod(dest, 0o640)

    src = _src(env, "mod.esp")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    assert dest.read_bytes() == b"mod content"
    rec = env.ownership[str(dest)]
    assert rec["had_original"] is True
    assert rec["orig_mode"] == 0o640
    # Backup exists, private, content-addressed
    backup_path = env.backups.path_for(rec["backup"])
    assert backup_path.read_bytes() == b"USER ORIGINAL"
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.backups.dir.stat().st_mode) == 0o700

    kept, problems, notes, cleared = _undeploy(env)
    assert kept == [] and cleared == 1
    assert dest.read_bytes() == b"USER ORIGINAL"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_preexisting_symlink_original_restored_exact_target(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    os.symlink("../relative/target.esp", dest)

    src = _src(env, "mod.esp")
    _deploy(env, [(src, Path("mod.esp"))])
    assert os.readlink(dest) == str(src.resolve())
    rec = env.ownership[str(dest)]
    assert rec["orig_link_target"] == "../relative/target.esp"
    assert rec["backup"] is None  # symlink originals need no content backup

    kept, problems, notes, cleared = _undeploy(env)
    assert kept == [] and cleared == 1
    assert dest.is_symlink()
    assert os.readlink(dest) == "../relative/target.esp"


def test_redeploy_backs_up_original_once(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")

    src1 = _src(env, "mod.esp", b"version one1")
    _deploy(env, [(src1, Path("mod.esp"))], method="copy")
    first_backup = env.ownership[str(dest)]["backup"]

    src2 = _src(env, "mod.esp", b"version two2")
    result = _deploy(env, [(src2, Path("mod.esp"))], method="copy")
    assert not result.errors
    rec = env.ownership[str(dest)]
    assert dest.read_bytes() == b"version two2"
    # Same original backup carried over; no second backup was created
    assert rec["backup"] == first_backup
    backups = [p.name for p in env.backups.dir.iterdir()]
    assert backups == [first_backup]


def test_dry_run_is_nonmutating(env: Env):
    src = _src(env, "mod.esp")
    plan = DeploymentPlan(data_files=[(src, Path("mod.esp"))])
    result = run_deploy(plan, env.game, method="symlink", dry_run=True)
    assert len(result.deployed) == 1
    assert list(env.game.iterdir()) == []  # nothing created, not even Data/
    assert not env.journal.exists()
    assert not env.backups.dir.exists()


def test_empty_parent_cleanup_bounded_by_data_and_root(env: Env):
    src = _src(env, "t.dds", b"texture")
    sfse = _src(env, "sfse_loader.exe", b"loader")
    _deploy(env, [(src, Path("textures/t.dds"))], root_files=[(sfse, Path("sfse_loader.exe"))])
    assert (env.game / "Data" / "textures" / "t.dds").exists()
    assert (env.game / "sfse_loader.exe").is_symlink()

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 2 and kept == []
    assert not (env.game / "Data" / "textures").exists()  # empty dir removed
    assert (env.game / "Data").exists()                   # Data itself kept
    assert env.game.exists()                              # game root kept


# ---------------------------------------------------------------------------
# External modification: refuse, never destroy
# ---------------------------------------------------------------------------

def test_same_size_same_mtime_edit_refused_on_undeploy(env: Env):
    src = _src(env, "mod.esp", b"mod content")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    dest = env.game / "Data" / "mod.esp"
    st = dest.stat()
    dest.write_bytes(b"MOD CONTENT")  # same length, different bytes
    os.utime(dest, (st.st_atime, st.st_mtime))  # same mtime too

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0
    assert len(kept) == 1
    assert any("refused" in p for p in problems)
    assert dest.read_bytes() == b"MOD CONTENT"  # user edit intact


def test_externally_replaced_symlink_refused(env: Env):
    src = _src(env, "mod.esp")
    _deploy(env, [(src, Path("mod.esp"))])
    dest = env.game / "Data" / "mod.esp"
    dest.unlink()
    os.symlink(str(env.tmp / "elsewhere"), dest)

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert os.readlink(dest) == str(env.tmp / "elsewhere")  # untouched

    # Repair: point it back at the recorded target, retry succeeds
    dest.unlink()
    os.symlink(str(src.resolve()), dest)
    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 1 and kept == []


def test_deploy_over_externally_modified_own_file_refused(env: Env):
    src = _src(env, "mod.esp", b"mod content")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    dest = env.game / "Data" / "mod.esp"
    dest.write_bytes(b"MOD CONTENT")

    src2 = _src(env, "mod2.esp", b"new content!")
    result = _deploy(env, [(src2, Path("mod.esp"))], method="copy")
    assert len(result.errors) == 1
    assert "refused" in result.errors[0]
    assert dest.read_bytes() == b"MOD CONTENT"
    # The stale ownership record is still in place for review
    assert str(dest) in env.ownership


def test_deploy_over_directory_refused(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.mkdir(parents=True)
    src = _src(env, "mod.esp")
    result = _deploy(env, [(src, Path("mod.esp"))])
    assert len(result.errors) == 1
    assert "not a regular file or symlink" in result.errors[0]
    assert dest.is_dir()


def test_missing_source_refused(env: Env):
    result = _deploy(env, [(env.staging / "ghost.esp", Path("ghost.esp"))])
    assert len(result.errors) == 1
    assert "source file is missing" in result.errors[0]


# ---------------------------------------------------------------------------
# Conflicts among mods
# ---------------------------------------------------------------------------

def test_conflict_deterministic_single_record_single_backup(env: Env):
    dest = env.game / "Data" / "x.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"ORIGINAL")
    orig_sha = hashlib.sha256(b"ORIGINAL").hexdigest()

    src_a = _src(env, "a/x.esp", b"from mod A")
    src_b = _src(env, "b/x.esp", b"from mod B")
    result = _deploy(env, [(src_a, Path("x.esp")), (src_b, Path("x.esp"))], method="copy")

    assert len(result.conflicts) == 1
    assert len(result.errors) == 0
    # Last file in plan order wins, exactly one ownership record
    assert dest.read_bytes() == b"from mod B"
    assert len(env.ownership) == 1
    rec = env.ownership[str(dest)]
    assert rec["src"] == str(src_b)
    # Original backed up exactly once
    assert rec["backup"] == orig_sha
    backups = [p.name for p in env.backups.dir.iterdir() if not p.name.startswith(".tmp")]
    assert backups == [orig_sha]


# ---------------------------------------------------------------------------
# Missing / corrupt backups: fail closed
# ---------------------------------------------------------------------------

def test_missing_backup_blocks_removal(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    src = _src(env, "mod.esp")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    rec = env.ownership[str(dest)]
    env.backups.path_for(rec["backup"]).unlink()  # lose the backup

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert any("missing or corrupt" in p for p in problems)
    # Fail closed: the deployed file is NOT removed without a restore path
    assert dest.read_bytes() == b"mod content"


def test_corrupt_backup_blocks_removal(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    src = _src(env, "mod.esp")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    rec = env.ownership[str(dest)]
    bp = env.backups.path_for(rec["backup"])
    bp.write_bytes(bp.read_bytes() + b"corruption")

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert dest.read_bytes() == b"mod content"  # deployed file untouched


# ---------------------------------------------------------------------------
# Legacy (pre-0.2.1) records
# ---------------------------------------------------------------------------

def test_legacy_symlink_matching_target_removed_with_note(env: Env):
    src = _src(env, "mod.esp")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    os.symlink(str(src.resolve()), dest)
    rec = {"src": str(src), "dest": str(dest), "method": "symlink"}
    env.ownership[str(dest)] = rec

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 1 and kept == []
    assert not dest.exists()
    assert any("pre-0.2.1" in n for n in notes)


def test_legacy_symlink_changed_target_refused(env: Env):
    src = _src(env, "mod.esp")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    os.symlink("/somewhere/else", dest)
    rec = {"src": str(src), "dest": str(dest), "method": "symlink"}
    env.ownership[str(dest)] = rec

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert os.readlink(dest) == "/somewhere/else"


def test_legacy_copy_content_match_removed(env: Env):
    src = _src(env, "mod.esp", b"same bytes")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"same bytes")
    rec = {"src": str(src), "dest": str(dest), "method": "copy"}
    env.ownership[str(dest)] = rec

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 1 and kept == []
    assert not dest.exists()


def test_legacy_copy_content_mismatch_refused(env: Env):
    src = _src(env, "mod.esp", b"staged bytes")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"user edited this")
    rec = {"src": str(src), "dest": str(dest), "method": "copy"}
    env.ownership[str(dest)] = rec

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert dest.read_bytes() == b"user edited this"


def test_legacy_copy_missing_source_refused(env: Env):
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"whatever")
    rec = {"src": str(env.staging / "gone.esp"), "dest": str(dest), "method": "copy"}
    env.ownership[str(dest)] = rec

    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert dest.exists()


# ---------------------------------------------------------------------------
# Containment: symlinked parents, outside roots, overlap
# ---------------------------------------------------------------------------

def test_symlinked_data_dir_refused_and_outside_untouched(env: Env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (env.game / "Data").symlink_to(outside)

    src = _src(env, "mod.esp")
    result = _deploy(env, [(src, Path("mod.esp"))])
    assert len(result.errors) == 1
    assert "symlinked parent" in result.errors[0]
    assert list(outside.iterdir()) == []  # nothing written through the link


def test_deep_symlinked_parent_refused(env: Env):
    outside = env.tmp / "outside"
    outside.mkdir()
    data = env.game / "Data"
    data.mkdir()
    (data / "meshes").symlink_to(outside)

    src = _src(env, "m.esp")
    result = _deploy(env, [(src, Path("meshes/m.esp"))])
    assert len(result.errors) == 1
    assert list(outside.iterdir()) == []


def test_record_outside_trusted_roots_refused(env: Env):
    outside_file = env.tmp / "user-file.txt"
    outside_file.write_bytes(b"mine")
    rec = {
        "src": str(_src(env, "s.esp")), "dest": str(outside_file),
        "method": "copy", "kind": "mod",
        "sha256": hashlib.sha256(b"mine").hexdigest(), "link_target": None,
        "had_original": False, "orig_sha256": None, "orig_link_target": None,
        "orig_mode": None, "backup": None, "ts": "",
    }
    env.ownership[str(outside_file)] = rec
    kept, problems, notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert outside_file.read_bytes() == b"mine"


def test_service_rejects_staging_game_overlap(svc, tmp_path):
    mods, game, prefix = _svc_env(tmp_path)
    # Same path
    result = svc.deploy(mods, game_dir=mods)
    assert result.deployed_count == 0
    assert any("same path" in e for e in result.errors)
    # Game inside staging
    sub = mods / "subgame"
    sub.mkdir()
    result = svc.deploy(mods, game_dir=sub)
    assert result.deployed_count == 0
    assert any("inside the staging directory" in e for e in result.errors)


def test_service_rejects_staging_inside_game(svc, tmp_path):
    game = tmp_path / "game2"
    mods = game / "mods"
    mods.mkdir(parents=True)
    (mods / "ModA").mkdir()
    (mods / "ModA" / "plugin.esp").write_bytes(b"x")
    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, DOMAIN)
    state.proton_prefix = str(tmp_path / "pfx")
    state.save()
    result = svc.deploy(mods, game_dir=game)
    assert result.deployed_count == 0
    assert any("inside the game directory" in e for e in result.errors)


def test_busy_lock_serializes_deploy_and_undeploy(svc, tmp_path):
    mods, game, prefix = _svc_env(tmp_path)
    with CollectionLock(mods):
        with pytest.raises(StateBusyError):
            svc.deploy(mods, game_dir=game)
        with pytest.raises(StateBusyError):
            svc.undeploy(mods)


# ---------------------------------------------------------------------------
# Journal replay / interruption windows (real files, no mocks)
# ---------------------------------------------------------------------------

def test_replay_intent_before_install_leaves_original(env: Env):
    src = _src(env, "mod.esp", b"new content")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    rec = _manual_record(env, src, dest, b"USER ORIGINAL")
    env.journal.deploy_intent(rec)  # crash before install

    ownership, reports = recover_ownership([], env.mods, [env.game])
    assert ownership == {}
    assert dest.read_bytes() == b"USER ORIGINAL"  # untouched
    assert any("original left in place" in r for r in reports)


def test_replay_intent_dest_deleted_in_window_restores_original(env: Env):
    src = _src(env, "mod.esp", b"new content")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    rec = _manual_record(env, src, dest, b"USER ORIGINAL")
    env.journal.deploy_intent(rec)  # backup secured; dest removed externally

    ownership, reports = recover_ownership([], env.mods, [env.game])
    assert ownership == {}
    assert dest.read_bytes() == b"USER ORIGINAL"  # restored from backup
    assert any("restored the original" in r for r in reports)


def test_replay_intent_external_file_in_window_kept_for_review(env: Env):
    src = _src(env, "mod.esp", b"new content")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    rec = _manual_record(env, src, dest, b"USER ORIGINAL")
    env.journal.deploy_intent(rec)
    dest.write_bytes(b"SOMEBODY ELSES FILE")  # created during the window

    ownership, reports = recover_ownership([], env.mods, [env.game])
    assert str(dest) in ownership  # record kept, retryable
    assert dest.read_bytes() == b"SOMEBODY ELSES FILE"  # never deleted
    assert any("unexpected content" in r for r in reports)


def test_replay_intent_after_install_folds_ownership(env: Env):
    src = _src(env, "mod.esp", b"deployed bytes")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"deployed bytes")  # install happened, done line lost
    rec = _manual_record(env, src, dest, None)
    env.journal.deploy_intent(rec)

    ownership, reports = recover_ownership([], env.mods, [env.game])
    assert ownership[str(dest)] == rec
    # The recovered record undeploys cleanly
    kept, problems, notes, cleared = undeploy_records(
        ownership, [env.game], env.backups, env.journal
    )
    assert cleared == 1 and kept == []
    assert not dest.exists()


def test_replay_midway_undeploy_restores_original(env: Env):
    src = _src(env, "mod.esp", b"mod content")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    rec = env.ownership[str(dest)]
    state_records = [dict(rec)]

    # Simulate crash: undeploy intent written, our file unlinked, restore lost
    env.journal.undeploy_intent(str(dest))
    dest.unlink()
    env.ownership = {}

    ownership, reports = recover_ownership(state_records, env.mods, [env.game])
    assert ownership == {}
    assert dest.read_bytes() == b"USER ORIGINAL"
    assert any("restored the original" in r for r in reports)


def test_replay_midway_undeploy_not_yet_removed_retries(env: Env):
    src = _src(env, "mod.esp", b"mod content")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    dest = env.game / "Data" / "mod.esp"
    rec = env.ownership[str(dest)]
    state_records = [dict(rec)]
    env.journal.undeploy_intent(str(dest))  # crash before the unlink
    env.ownership = {}

    ownership, reports = recover_ownership(state_records, env.mods, [env.game])
    assert str(dest) in ownership  # kept for retry
    assert any("retried" in r or "retry" in r for r in reports)

    kept, problems, notes, cleared = undeploy_records(
        ownership, [env.game], env.backups, env.journal
    )
    assert cleared == 1 and kept == []
    assert not dest.exists()


def test_intent_written_before_install_mutates(env: Env, monkeypatch):
    """The journal intent line must be on disk before the destination moves."""
    seen = []
    real_install = deploy_module.install_deploy

    def observing_install(rec, src, current, backups):
        lines = [json.loads(x) for x in env.journal.path.read_text().splitlines()]
        intents = [e for e in lines
                   if e.get("op") == "deploy_intent"
                   and e.get("record", {}).get("dest") == rec["dest"]]
        seen.append(len(intents))
        return real_install(rec, src, current, backups)

    monkeypatch.setattr(deploy_module, "install_deploy", observing_install)
    src = _src(env, "mod.esp")
    _deploy(env, [(src, Path("mod.esp"))])
    assert seen == [1]


def test_install_failure_leaves_recoverable_journal(env: Env, monkeypatch):
    real_install = deploy_module.install_deploy

    def failing_install(rec, src, current, backups):
        if Path(rec["dest"]).name == "bad.esp":
            raise OSError(5, "injected install failure")
        return real_install(rec, src, current, backups)

    monkeypatch.setattr(deploy_module, "install_deploy", failing_install)
    good = _src(env, "good.esp", b"good")
    bad = _src(env, "bad.esp", b"bad")
    result = _deploy(env, [(good, Path("good.esp")), (bad, Path("bad.esp"))])
    assert len(result.deployed) == 1
    assert len(result.errors) == 1 and "bad.esp" in result.errors[0]

    # Dangling intent for bad.esp resolves cleanly (nothing was installed)
    monkeypatch.setattr(deploy_module, "install_deploy", real_install)
    records = list(env.ownership.values())
    ownership, reports = recover_ownership(records, env.mods, [env.game])
    assert len(ownership) == 1  # only good.esp tracked
    kept, problems, notes, cleared = undeploy_records(
        ownership, [env.game], env.backups, env.journal
    )
    assert cleared == 1 and kept == []


def test_no_cross_directory_renames_during_cycle(env: Env, monkeypatch):
    """Backups/restores/publishes must never assume os.replace across mounts."""
    renames = []
    real_replace = os.replace

    def recording_replace(src, dst, **kwargs):
        renames.append((Path(str(src)).parent, Path(str(dst)).parent))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    src = _src(env, "mod.esp", b"mod content")
    _deploy(env, [(src, Path("mod.esp"))], method="copy")
    _undeploy(env)

    assert renames, "expected staged renames during deploy/undeploy"
    for src_parent, dst_parent in renames:
        assert src_parent == dst_parent, (
            f"rename across directories (potential cross-device): "
            f"{src_parent} -> {dst_parent}"
        )
    assert dest.read_bytes() == b"USER ORIGINAL"


# ---------------------------------------------------------------------------
# Service-level integration: plugins.txt / INI / partial failure / recovery
# ---------------------------------------------------------------------------

def test_service_deploy_undeploy_full_cycle(svc, tmp_path):
    mods, game, prefix = _svc_env(tmp_path)
    result = svc.deploy(mods, game_dir=game)
    assert result.deployed_count == 1, result.errors
    assert not result.errors

    dest = game / "Data" / "ModA" / "plugin.esp"
    assert dest.is_symlink()
    ini_path = get_game_ini_path(prefix, DOMAIN)
    assert ini_path.exists()
    ini_text = ini_path.read_text()
    assert "bInvalidateOlderFiles=1" in ini_text

    state = CollectionState(mods)
    state.load()
    kinds = {r["kind"] for r in state.deployed_files}
    assert "mod" in kinds and "ini" in kinds
    assert state.deployed_at
    assert not (mods / ".nexus-journal.jsonl").exists()  # cleared after save

    removed = svc.undeploy(mods)
    assert removed >= 1
    assert not dest.exists()
    assert not (game / "Data" / "ModA").exists()
    assert not ini_path.exists()  # INI created by us: removed on undeploy
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files == []


def test_service_preserves_original_plugins_and_ini(svc, tmp_path):
    mods, game, prefix = _svc_env(
        tmp_path, staging_files={"ModA/plugin.esp": b"esp", "plugins.txt": b"*ModA.esp\n"}
    )
    plugins_dest = get_plugins_txt_dest(prefix, DOMAIN)
    plugins_dest.parent.mkdir(parents=True)
    plugins_dest.write_bytes(b"USER PLUGINS\n")
    os.chmod(plugins_dest, 0o640)
    ini_path = get_game_ini_path(prefix, DOMAIN)
    ini_path.parent.mkdir(parents=True)
    ini_path.write_bytes(b"[Display]\niSize H=1080\n")

    result = svc.deploy(mods, game_dir=game)
    assert not result.errors
    assert plugins_dest.read_bytes() == b"*ModA.esp\n"
    merged = ini_path.read_text()
    assert "[Display]" in merged and "iSize H=1080" in merged  # user keys kept
    assert "bInvalidateOlderFiles=1" in merged

    svc.undeploy(mods)
    assert plugins_dest.read_bytes() == b"USER PLUGINS\n"
    assert stat.S_IMODE(plugins_dest.stat().st_mode) == 0o640
    assert ini_path.read_bytes() == b"[Display]\niSize H=1080\n"


def test_service_deploy_failure_skips_plugins_and_ini(svc, tmp_path):
    mods, game, prefix = _svc_env(
        tmp_path, staging_files={"ModA/plugin.esp": b"esp", "plugins.txt": b"*ModA.esp\n"}
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (game / "Data").symlink_to(outside)  # forces every data-file deploy to refuse

    result = svc.deploy(mods, game_dir=game)
    assert result.deployed_count == 0
    assert result.errors
    assert not get_game_ini_path(prefix, DOMAIN).exists()
    assert not get_plugins_txt_dest(prefix, DOMAIN).exists()
    assert list(outside.iterdir()) == []


def test_service_redeploy_stops_on_unsafe_removal(svc, tmp_path):
    mods, game, prefix = _svc_env(tmp_path)
    result = svc.deploy(mods, game_dir=game, use_copy=True)
    assert not result.errors
    dest = game / "Data" / "ModA" / "plugin.esp"
    dest.write_bytes(b"ESP BYTES")  # same size as b"esp bytes", user edit

    result2 = svc.deploy(mods, game_dir=game, use_copy=True)
    assert result2.deployed_count == 0
    assert any("Redeploy stopped" in e for e in result2.errors)
    assert dest.read_bytes() == b"ESP BYTES"  # user edit intact
    state = CollectionState(mods)
    state.load()
    mod_records = [r for r in state.deployed_files if r["kind"] == "mod"]
    assert len(mod_records) == 1  # refused entry retained for retry


def test_service_undeploy_partial_raises_and_retry_succeeds(svc, tmp_path):
    mods, game, prefix = _svc_env(tmp_path)
    result = svc.deploy(mods, game_dir=game, use_copy=True)
    assert not result.errors
    dest = game / "Data" / "ModA" / "plugin.esp"
    dest.write_bytes(b"ESP BYTES")  # external same-size edit

    with pytest.raises(UndeployError) as excinfo:
        svc.undeploy(mods)
    err = excinfo.value
    assert len(err.refused) == 1 and str(dest) in err.refused[0]
    assert dest.read_bytes() == b"ESP BYTES"  # untouched
    state = CollectionState(mods)
    state.load()
    assert any(r["dest"] == str(dest) for r in state.deployed_files)

    # Repair the file to the deployed content and retry: clean success
    dest.write_bytes(b"esp bytes")
    removed = svc.undeploy(mods)
    assert removed == 1
    assert not dest.exists()
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files == []


def test_service_state_save_failure_after_install_recovers(svc, tmp_path, monkeypatch):
    mods, game, prefix = _svc_env(tmp_path)
    real_save = CollectionState.save

    def failing_save(self):
        raise StateError("injected state save failure")

    monkeypatch.setattr(CollectionState, "save", failing_save)
    with pytest.raises(StateError):
        svc.deploy(mods, game_dir=game)
    monkeypatch.setattr(CollectionState, "save", real_save)

    # State on disk has no deployment, but the journal and files exist
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files == []
    assert (mods / ".nexus-journal.jsonl").exists()
    dest = game / "Data" / "ModA" / "plugin.esp"
    assert dest.is_symlink()

    # Next deploy recovers ownership from the journal, removes the old
    # deployment safely, and redeploys.
    result = svc.deploy(mods, game_dir=game)
    assert result.deployed_count == 1, result.errors
    assert dest.is_symlink()
    state = CollectionState(mods)
    state.load()
    assert any(r["dest"] == str(dest) for r in state.deployed_files)

    removed = svc.undeploy(mods)
    assert removed >= 1
    assert not dest.exists()


def test_service_nothing_to_deploy_keeps_existing_deployment(svc, tmp_path):
    mods, game, prefix = _svc_env(tmp_path)
    result = svc.deploy(mods, game_dir=game)
    assert result.deployed_count == 1

    # Empty the staging content; redeploy must not tear down the deployment
    (mods / "ModA" / "plugin.esp").unlink()
    result2 = svc.deploy(mods, game_dir=game)
    assert result2.deployed_count == 0
    assert any("left in place" in e for e in result2.errors)
    dest = game / "Data" / "ModA" / "plugin.esp"
    assert dest.is_symlink()  # still deployed
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files  # ownership retained


# ---------------------------------------------------------------------------
# Bugcheck regressions: backup-name hygiene (H1), recovery roots (H2),
# atomic publish (C1), staged-restore verification (M), journal retention
# ---------------------------------------------------------------------------

def test_backup_name_traversal_and_absolute_rejected(env: Env):
    """H1: corrupt or malicious backup references never steer IO outside the store."""
    store = env.backups
    victim = env.tmp / "victim.txt"
    victim.write_bytes(b"keep me")

    for bad in ["../victim.txt", "../../evil", "/etc/passwd", "short",
                "g" * 64, "A" * 64, "", None, 42]:
        with pytest.raises(DeployError):
            store.path_for(bad)
        assert store.verify(bad, "0" * 64) is False
        store.drop(bad)  # invalid names are ignored, never resolved
    assert victim.read_bytes() == b"keep me"

    # Integration: a tampered ownership record with a traversal backup name
    # is refused on undeploy; deployed and outside files stay untouched.
    src = _src(env, "mod.esp", b"mod content")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"mod content")  # fingerprint matches "deployed"
    rec = _manual_record(env, src, dest, original=b"orig bytes")
    rec["backup"] = "../victim.txt"
    kept, problems, _notes, cleared = undeploy_records(
        {str(dest): rec}, [env.game], env.backups, env.journal
    )
    assert cleared == 0 and len(kept) == 1
    assert any("missing or corrupt" in p for p in problems)
    assert dest.read_bytes() == b"mod content"
    assert victim.read_bytes() == b"keep me"


def test_recovery_rejects_malicious_backup_reference(env: Env):
    """A journal record with a traversal backup name restores nothing."""
    victim = env.tmp / "victim.txt"
    victim.write_bytes(b"keep me")
    dest = env.game / "Data" / "ghost.esp"  # absent
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = _src(env, "mod.esp")
    rec = _manual_record(env, src, dest, original=b"payload")
    rec["backup"] = "../victim.txt"
    env.journal.deploy_intent(rec)

    ownership, reports = recover_ownership([], env.mods, [env.game])
    assert not dest.exists()
    assert str(dest) in ownership  # kept for review, never dropped
    assert any("missing or corrupt" in r for r in reports)
    assert victim.read_bytes() == b"keep me"


def test_recovery_refuses_restores_outside_roots_and_symlinked_parents(env: Env):
    """H2: recovery mutations are containment-validated against trusted roots
    derived from configuration, never from journal content."""
    outside = env.tmp / "outside"
    outside.mkdir()
    target = outside / "restore-target.txt"  # absent; naive recovery would write here

    outside2 = env.tmp / "outside2"
    outside2.mkdir()
    link = env.game / "Data" / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside2, link)
    sneaky = link / "file.esp"  # lexically in root, physically outside

    src = _src(env, "mod.esp")
    rec_out = _manual_record(env, src, target, original=b"payload-out")
    rec_link = _manual_record(env, src, sneaky, original=b"payload-link")
    env.journal.deploy_intent(rec_out)
    env.journal.deploy_intent(rec_link)

    ownership, reports = recover_ownership([], env.mods, [env.game])

    assert not target.exists()            # never written outside the root
    assert list(outside2.iterdir()) == []  # never written through the symlink
    assert str(target) in ownership        # both kept for review
    assert str(sneaky) in ownership
    assert any("refusing to restore" in r for r in reports)
    assert any(str(sneaky) in r for r in reports)


def test_copy_publish_failure_leaves_destination_untouched(env: Env, monkeypatch):
    """C1: a failure at publish time never unlinks or truncates the destination."""
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    src = _src(env, "mod.esp", b"mod content")

    real_replace = os.replace

    def failing_replace(s, d, **kw):
        if Path(str(d)) == dest:
            raise OSError(5, "injected publish failure")
        return real_replace(s, d, **kw)

    monkeypatch.setattr(os, "replace", failing_replace)
    result = _deploy(env, [(src, Path("mod.esp"))], method="copy")
    monkeypatch.setattr(os, "replace", real_replace)

    assert result.deployed == [] and len(result.errors) == 1
    assert dest.read_bytes() == b"USER ORIGINAL"  # never removed or truncated
    assert not env.ownership                       # failed file untracked
    assert not list(dest.parent.glob(".nexus-deploy-*"))  # temp cleaned up
    assert list(env.backups.dir.iterdir())         # original backup retained

    # Dangling intent resolves to "original left in place"; nothing mutated
    ownership, reports = recover_ownership([], env.mods, [env.game])
    assert ownership == {}
    assert dest.read_bytes() == b"USER ORIGINAL"
    assert any("original left in place" in r for r in reports)


def test_symlink_publish_failure_leaves_destination_untouched(env: Env, monkeypatch):
    """C1: the symlink path stages a temp link; publish failure touches nothing."""
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")
    src = _src(env, "mod.esp", b"mod content")

    real_replace = os.replace

    def failing_replace(s, d, **kw):
        if Path(str(d)) == dest:
            raise OSError(5, "injected publish failure")
        return real_replace(s, d, **kw)

    monkeypatch.setattr(os, "replace", failing_replace)
    result = _deploy(env, [(src, Path("mod.esp"))], method="symlink")
    monkeypatch.setattr(os, "replace", real_replace)

    assert len(result.errors) == 1 and result.deployed == []
    assert dest.read_bytes() == b"USER ORIGINAL"
    assert not dest.is_symlink()
    assert not list(dest.parent.glob(".nexus-link-*"))  # staged link cleaned


def test_service_deploy_publish_failure_preserves_journal_and_recovers(
    svc, tmp_path, monkeypatch
):
    """Real service.deploy failing mid-publish: journal kept, failed dest never
    enters ownership, retry completes cleanly, undeploy restores original bytes."""
    mods, game, prefix = _svc_env(
        tmp_path,
        staging_files={"ModA/plugin.esp": b"esp a", "ModB/other.esp": b"esp b"},
    )
    orig_b = game / "Data" / "ModB" / "other.esp"
    orig_b.parent.mkdir(parents=True)
    orig_b.write_bytes(b"USER ORIGINAL B")

    real_replace = os.replace

    def failing_replace(s, d, **kw):
        if Path(str(d)) == orig_b:
            raise OSError(5, "injected publish failure")
        return real_replace(s, d, **kw)

    monkeypatch.setattr(os, "replace", failing_replace)
    result = svc.deploy(mods, game_dir=game)
    monkeypatch.setattr(os, "replace", real_replace)

    # Failed run: error surfaced, destination untouched, journal NOT cleared
    assert result.errors and any(str(orig_b) in e for e in result.errors)
    assert orig_b.read_bytes() == b"USER ORIGINAL B"
    assert (mods / ".nexus-journal.jsonl").exists()
    state = CollectionState(mods)
    state.load()
    dests = {r["dest"] for r in state.deployed_files}
    assert str(game / "Data" / "ModA" / "plugin.esp") in dests
    assert str(orig_b) not in dests  # failed file never entered ownership

    # Retry without the fault: recovery folds the dangling intent, deploy
    # completes cleanly, and only then is the journal discarded.
    result2 = svc.deploy(mods, game_dir=game)
    assert not result2.errors, result2.errors
    state = CollectionState(mods)
    state.load()
    dests = {r["dest"] for r in state.deployed_files}
    assert str(orig_b) in dests
    assert not (mods / ".nexus-journal.jsonl").exists()

    # Undeploy restores the user's original bytes exactly.
    removed = svc.undeploy(mods)
    assert removed >= 2
    assert orig_b.read_bytes() == b"USER ORIGINAL B"
    assert not (game / "Data" / "ModA" / "plugin.esp").exists()


def test_service_lost_done_line_keeps_state_and_undeploys(svc, tmp_path, monkeypatch):
    """Ownership lands in saved state even when journal done lines are lost."""
    mods, game, prefix = _svc_env(tmp_path)
    real_done = DeployJournal.deploy_done

    def failing_done(self, dest_str):
        raise OSError(28, "injected ENOSPC on journal completion line")

    monkeypatch.setattr(DeployJournal, "deploy_done", failing_done)
    result = svc.deploy(mods, game_dir=game)
    monkeypatch.setattr(DeployJournal, "deploy_done", real_done)

    assert not result.errors, result.errors
    dest = game / "Data" / "ModA" / "plugin.esp"
    assert dest.is_symlink()
    state = CollectionState(mods)
    state.load()
    assert any(r["dest"] == str(dest) for r in state.deployed_files)

    # The deployment is fully tracked from state alone: undeploy is clean.
    removed = svc.undeploy(mods)
    assert removed >= 1
    assert not dest.exists()
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files == []


def test_service_redeploy_stop_preserves_journal(svc, tmp_path):
    """A redeploy that stops on an unsafe removal keeps the dangling undeploy
    intent recoverable; repairing and redeploying finishes cleanly."""
    mods, game, prefix = _svc_env(tmp_path)
    result = svc.deploy(mods, game_dir=game, use_copy=True)
    assert not result.errors
    dest = game / "Data" / "ModA" / "plugin.esp"
    dest.write_bytes(b"ESP BYTES")  # external same-size edit

    result2 = svc.deploy(mods, game_dir=game, use_copy=True)
    assert any("Redeploy stopped" in e for e in result2.errors)
    assert (mods / ".nexus-journal.jsonl").exists()
    assert DeployJournal(mods).read()  # dangling entries retained, not cleared
    assert dest.read_bytes() == b"ESP BYTES"  # untouched
    state = CollectionState(mods)
    state.load()
    assert any(r["dest"] == str(dest) for r in state.deployed_files)

    # Repair to the deployed content and redeploy: clean success
    dest.write_bytes(b"esp bytes")
    result3 = svc.deploy(mods, game_dir=game, use_copy=True)
    assert not result3.errors, result3.errors
    assert not (mods / ".nexus-journal.jsonl").exists()
    assert dest.read_bytes() == b"esp bytes"

    removed = svc.undeploy(mods)
    assert removed >= 1
    assert not dest.exists()


def test_service_interrupted_undeploy_retains_entries_and_journal(
    svc, tmp_path, monkeypatch
):
    """An interrupted removal keeps both the state entry and the journal."""
    mods, game, prefix = _svc_env(tmp_path)
    result = svc.deploy(mods, game_dir=game)
    assert not result.errors
    dest = game / "Data" / "ModA" / "plugin.esp"

    real_unlink = os.unlink

    def failing_unlink(path, *a, **kw):
        if str(path) == str(dest):
            raise OSError(5, "injected unlink failure")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    with pytest.raises(UndeployError) as excinfo:
        svc.undeploy(mods)
    monkeypatch.setattr(os, "unlink", real_unlink)

    err = excinfo.value
    assert len(err.failed) == 1 and str(dest) in err.failed[0]
    assert (mods / ".nexus-journal.jsonl").exists()  # dangling intent retained
    assert dest.exists()
    state = CollectionState(mods)
    state.load()
    assert any(r["dest"] == str(dest) for r in state.deployed_files)

    # Retry with the fault gone: recovery folds the intent, removal completes
    removed = svc.undeploy(mods)
    assert removed >= 1
    assert not dest.exists()
    assert not (mods / ".nexus-journal.jsonl").exists()
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files == []


def test_service_corrupt_backup_mid_restore_never_replaces_deployed_file(
    svc, tmp_path, monkeypatch
):
    """M: the staged restore copy is re-hashed before publish; a backup that
    turns corrupt after pre-verification leaves the deployed file in place."""
    mods, game, prefix = _svc_env(tmp_path)
    dest = game / "Data" / "ModA" / "plugin.esp"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"USER ORIGINAL")

    result = svc.deploy(mods, game_dir=game, use_copy=True)
    assert not result.errors, result.errors
    assert dest.read_bytes() == b"esp bytes"

    # Corrupt the backup blob but let pre-verification pass (TOCTOU window
    # between verify and copy).
    blob = mods / ".nexus-backups" / hashlib.sha256(b"USER ORIGINAL").hexdigest()
    assert blob.is_file()
    blob.write_bytes(b"corrupted blob")
    real_verify = BackupStore.verify
    monkeypatch.setattr(BackupStore, "verify", lambda self, name, sha: True)

    with pytest.raises(UndeployError) as excinfo:
        svc.undeploy(mods)
    monkeypatch.setattr(BackupStore, "verify", real_verify)

    err = excinfo.value
    assert len(err.failed) == 1
    assert "failed verification during restore" in err.failed[0]
    assert dest.read_bytes() == b"esp bytes"  # deployed file left in place
    assert not list(dest.parent.glob(".nexus-restore-*"))  # no staged remnants
    state = CollectionState(mods)
    state.load()
    assert any(r["dest"] == str(dest) for r in state.deployed_files)
    assert (mods / ".nexus-journal.jsonl").exists()

    # Repair the blob and retry: the original bytes are restored exactly.
    blob.write_bytes(b"USER ORIGINAL")
    removed = svc.undeploy(mods)
    assert removed >= 1
    assert dest.read_bytes() == b"USER ORIGINAL"
    state = CollectionState(mods)
    state.load()
    assert state.deployed_files == []


def test_symlink_deploy_replaced_by_matching_regular_file_refused(env: Env):
    """Positive per-kind ownership: a symlink deployment externally replaced
    by a regular file is never ours, even when the bytes match the staging
    source (the pre-fix source-content fallback treated it as deployed)."""
    src = _src(env, "mod.esp", b"mod content")
    _deploy(env, [(src, Path("mod.esp"))], method="symlink")
    dest = env.game / "Data" / "mod.esp"
    assert dest.is_symlink()

    dest.unlink()
    dest.write_bytes(b"mod content")  # external regular file, staging bytes

    rec = env.ownership[str(dest)]
    assert fingerprint_state(dest, rec)[0] == "unknown"

    # Undeploy refuses and leaves the external file alone
    kept, problems, _notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert any("changed outside" in p for p in problems)
    assert dest.read_bytes() == b"mod content"
    assert not dest.is_symlink()

    # Redeploy refuses to replace it as well
    result = _deploy(env, [(src, Path("mod.esp"))], method="symlink")
    assert len(result.errors) == 1 and "changed outside" in result.errors[0]
    assert dest.read_bytes() == b"mod content"
    assert not dest.is_symlink()


def test_symlink_over_original_symlink_replaced_by_regular_file_refused(env: Env):
    """Same guarantee when the recorded original was itself a symlink (the
    record has neither sha256 nor orig_sha256): a regular-file replacement
    matching staging bytes is external content, never ours."""
    user_target = env.tmp / "user-target.txt"
    user_target.write_bytes(b"user target content")
    dest = env.game / "Data" / "mod.esp"
    dest.parent.mkdir(parents=True)
    os.symlink(user_target, dest)

    src = _src(env, "mod.esp", b"mod content")
    _deploy(env, [(src, Path("mod.esp"))], method="symlink")
    assert dest.is_symlink()
    rec = env.ownership[str(dest)]
    assert rec["had_original"] and rec["orig_link_target"] == str(user_target)
    assert rec["sha256"] is None and rec["orig_sha256"] is None

    dest.unlink()
    dest.write_bytes(b"mod content")  # external regular file, staging bytes
    assert fingerprint_state(dest, rec)[0] == "unknown"

    kept, problems, _notes, cleared = _undeploy(env)
    assert cleared == 0 and len(kept) == 1
    assert any("changed outside" in p for p in problems)
    assert dest.read_bytes() == b"mod content"
    assert not dest.is_symlink()

    result = _deploy(env, [(src, Path("mod.esp"))], method="symlink")
    assert len(result.errors) == 1 and "changed outside" in result.errors[0]
    assert dest.read_bytes() == b"mod content"
    assert not dest.is_symlink()
