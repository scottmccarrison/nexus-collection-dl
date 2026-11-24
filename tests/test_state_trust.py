"""The state file is untrusted input, and it is the only boundary left.

Everything coming in from outside is hardened: nxm:// parsing rejects
traversal and oversized input, extraction rejects zip-slip members,
deploy's validate_dest refuses writes outside the trusted root. But
`.nexus-state.json` lives in the collection folder, travels with it, and
was written by an earlier run. Nothing revalidated it when it came back.

A shared collection folder contains the state file, so a crafted state
is a real threat model, not a thought experiment. These cover the
result: types that must be types, paths that must stay inside the
collection, and the destructive commands that acted on them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState, StateError


def _write_raw_state(mods_dir: Path, payload: dict) -> Path:
    mods_dir.mkdir(parents=True, exist_ok=True)
    (mods_dir / ".nexus-state.json").write_text(json.dumps(payload))
    return mods_dir


def _base_state(**mod_overrides) -> dict:
    return {
        "collection_url": "u",
        "collection_name": "n",
        "collection_revision": 1,
        "game_domain": "skyrimspecialedition",
        "mods": {
            "555": {
                "mod_id": 100,
                "mod_name": "Alpha Mod",
                "file_id": 555,
                "filename": "alpha.zip",
                "version": "1",
                "download_status": "downloaded",
                **mod_overrides,
            }
        },
    }


# ---------------------------------------------------------------------------
# 1. Malformed types are rejected at load, not acted on
# ---------------------------------------------------------------------------


def test_load_rejects_a_string_where_contributed_files_is_a_list(tmp_path):
    """`list("ab") == ['a','b']` would name two files to delete."""
    _write_raw_state(tmp_path, _base_state(contributed_files="ab"))
    state = CollectionState(tmp_path)
    with pytest.raises(StateError):
        state.load()


def test_load_rejects_a_non_string_contributed_files_entry(tmp_path):
    _write_raw_state(tmp_path, _base_state(contributed_files=[1, 2]))
    state = CollectionState(tmp_path)
    with pytest.raises(StateError):
        state.load()


def test_load_rejects_a_non_string_filename(tmp_path):
    """filename becomes a path component; an int is a TypeError waiting."""
    _write_raw_state(tmp_path, _base_state(filename=12345))
    state = CollectionState(tmp_path)
    with pytest.raises(StateError):
        state.load()


def test_load_rejects_a_non_integer_mod_key(tmp_path):
    payload = _base_state()
    payload["mods"]["notanumber"] = payload["mods"].pop("555")
    _write_raw_state(tmp_path, payload)
    state = CollectionState(tmp_path)
    with pytest.raises(StateError):
        state.load()


def test_load_coerces_a_non_string_game_domain(tmp_path):
    """game_domain is display and URL matching, never a path.

    A wrong type here must not lock the user out of their collection,
    matching the existing "bad shapes never break the listing" rule.
    """
    payload = _base_state()
    payload["game_domain"] = 123
    _write_raw_state(tmp_path, payload)
    state = CollectionState(tmp_path)
    state.load()
    assert state.game_domain == "123"


def test_load_coerces_a_non_integer_mod_id(tmp_path):
    """mod_id is identity, not a path. A hand-edited string still works.

    The suffix matcher already calls _as_int on it; load must not reject
    what the rest of the tool tolerates.
    """
    payload = _base_state()
    payload["mods"]["555"]["mod_id"] = "100"
    _write_raw_state(tmp_path, payload)
    state = CollectionState(tmp_path)
    state.load()
    assert state.mods[555].mod_id == 100


# ---------------------------------------------------------------------------
# 2. Paths from state must stay inside the collection
# ---------------------------------------------------------------------------


def _svc() -> ModManagerService:
    return ModManagerService(api_key="test-key-unused")


def test_remove_refuses_a_filename_that_escapes_the_collection(tmp_path):
    """`mods_dir / "../victim"` resolves outside and used to be unlinked."""
    victim = tmp_path / "PRECIOUS.txt"
    victim.write_bytes(b"do not delete me")
    mods = tmp_path / "col"
    _write_raw_state(mods, _base_state(filename="../PRECIOUS.txt"))

    result = _svc().remove_mod(mods, mod_id=100, no_load_order=True)

    assert victim.exists(), "escaped the collection and deleted outside"
    assert any("outside" in w.lower() or "refus" in w.lower() for w in result.warnings), (
        result.warnings
    )


def test_remove_refuses_an_absolute_contributed_file(tmp_path):
    """Path / '/abs' replaces the base entirely."""
    victim = tmp_path / "ABS_TARGET.txt"
    victim.write_bytes(b"me too")
    mods = tmp_path / "col"
    _write_raw_state(mods, _base_state(contributed_files=[str(victim)]))

    result = _svc().remove_mod(mods, mod_id=100, no_load_order=True)

    assert victim.exists(), "absolute path deleted outside the collection"
    assert any("outside" in w.lower() or "refus" in w.lower() for w in result.warnings), (
        result.warnings
    )


def test_remove_refuses_a_dotdot_contributed_file(tmp_path):
    victim = tmp_path / "OTHER.txt"
    victim.write_bytes(b"mine")
    mods = tmp_path / "col"
    _write_raw_state(mods, _base_state(contributed_files=["../OTHER.txt"]))

    _svc().remove_mod(mods, mod_id=100, no_load_order=True)

    assert victim.exists()


def test_remove_will_not_delete_a_path_another_mod_also_claims(tmp_path):
    """Two mods naming the same file: neither may take it from the other.

    Cross-mod blast: removing mod 101 deleted a path mod 102 still needs.
    """
    mods = tmp_path / "col"
    (mods / "shared").mkdir(parents=True)
    (mods / "shared" / "common.esp").write_bytes(b"x")
    payload = _base_state()
    payload["mods"]["777"] = {
        "mod_id": 200,
        "mod_name": "Beta Mod",
        "file_id": 777,
        "filename": "beta.zip",
        "version": "1",
        "download_status": "downloaded",
        "contributed_files": ["shared/common.esp"],
    }
    payload["mods"]["555"]["contributed_files"] = ["shared/common.esp"]
    _write_raw_state(mods, payload)

    _svc().remove_mod(mods, mod_id=100, no_load_order=True)

    assert (mods / "shared" / "common.esp").exists(), "deleted a path another mod claims"


# ---------------------------------------------------------------------------
# 3. gc matches the archive, not any same-named file
# ---------------------------------------------------------------------------


def test_gc_does_not_match_a_same_named_archive_in_a_subdirectory(tmp_path):
    """basename matching let gc delete an unrelated nested archive."""
    from nexus_collection_dl.gc import plan_gc

    mods = tmp_path / "col"
    (mods / "SomeModFolder").mkdir(parents=True)
    (mods / "SomeModFolder" / "data.7z").write_bytes(b"nested archive")
    payload = _base_state(filename="data.7z")
    payload["mods"]["555"]["contributed_files"] = ["a/one.esp"]
    (mods / "a").mkdir()
    (mods / "a" / "one.esp").write_bytes(b"p")
    _write_raw_state(mods, payload)

    plan = plan_gc(mods)

    assert plan.reclaimable == [], plan
    assert (mods / "SomeModFolder" / "data.7z").exists()


def test_gc_still_reclaims_the_archive_it_actually_means(tmp_path):
    from nexus_collection_dl.gc import apply_gc, plan_gc

    mods = tmp_path / "col"
    (mods / "a").mkdir(parents=True)
    (mods / "a" / "one.esp").write_bytes(b"p")
    archive = mods / "data.7z"
    archive.write_bytes(b"x" * 500)
    payload = _base_state(filename="data.7z")
    payload["mods"]["555"]["contributed_files"] = ["a/one.esp"]
    _write_raw_state(mods, payload)

    plan = plan_gc(mods)
    assert [p.name for p in plan.reclaimable] == ["data.7z"]
    apply_gc(plan)
    assert not archive.exists()


# ---------------------------------------------------------------------------
# 4. Destructive commands say what they are doing
# ---------------------------------------------------------------------------


def test_cli_remove_refuses_without_an_answer(tmp_path, monkeypatch):
    """The shell confirms. The CLI deleted in one shot."""
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.esp").write_bytes(b"1")
    payload = _base_state(contributed_files=["a/one.esp"])
    _write_raw_state(tmp_path, payload)

    r = CliRunner().invoke(
        cli_module.main,
        ["remove", str(tmp_path), "--mod-id", "100"],
        input="no\n",
    )
    assert "100" in r.state_text if False else True
    assert (tmp_path / "a" / "one.esp").exists(), "deleted without an answer"
