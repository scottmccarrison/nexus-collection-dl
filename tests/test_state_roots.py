"""State-supplied roots are not their own trust anchor.

The state file cannot vouch for itself. `deployed_files` dest paths are
already contained by validate_dest, but the ROOT that containment runs
against comes from the same file. A crafted `game_dir: "/"` passes every
check, because `/` genuinely is a root directory, and anything whose
fingerprint still matches is then unlinked wherever it points.

These cover the rule that must hold whatever mechanism enforces it:
a destructive command never acts outside the roots the user actually
established (an explicit flag, the user config, or the machine's own
game detection) - never the state file's word alone.
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

import pytest

from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState

DOMAIN = "skyrimspecialedition"


def _record(dest: Path, content: bytes) -> dict:
    """An ownership record claiming we deployed this content to dest."""
    return {
        "src": "/nonexistent/staging/x.esp",
        "dest": str(dest),
        "method": "copy",
        "kind": "mod",
        "sha256": hashlib.sha256(content).hexdigest(),
        "link_target": None,
        "had_original": False,
        "orig_sha256": None,
        "orig_link_target": None,
        "orig_mode": 0o644,
        "backup": None,
        "ts": "2026-10-29T00:00:00+00:00",
    }


def _write_state(mods_dir: Path, game_dir: str, deployed_files: list[dict]) -> None:
    mods_dir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(mods_dir)
    state.set_collection_info("u", "n", 1, DOMAIN)
    state.game_dir = game_dir
    state.deployed_files = deployed_files
    state.save()


def _svc() -> ModManagerService:
    return ModManagerService(api_key="test-key-unused")


@pytest.fixture(autouse=True)
def no_machine(monkeypatch, tmp_path):
    """Neither the user config nor the machine's Steam may speak here.

    These tests are about what the state file alone can cause. A real
    Steam library would resolve a root and make the run pass for the
    wrong reason.
    """
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))


# ---------------------------------------------------------------------------
# The root is the hole: containment is only as good as its base
# ---------------------------------------------------------------------------


def test_undeploy_does_not_delete_through_a_state_root_the_user_never_established(
    tmp_path
):
    """A crafted game_dir turned the whole filesystem into a trusted root.

    The dest is contained (it is inside game_dir) and the fingerprint
    matches, so every existing check passes and the file is unlinked.
    """
    victim = tmp_path / "PRECIOUS.txt"
    victim.write_bytes(b"do not delete me")
    mods = tmp_path / "col"
    _write_state(
        mods,
        game_dir=str(tmp_path),
        deployed_files=[_record(victim, victim.read_bytes())],
    )

    # Refusing is a valid outcome; deleting is not.
    with contextlib.suppress(Exception):
        _svc().undeploy(mods)

    assert victim.exists(), "undeploy deleted outside the collection through a crafted game_dir"


def test_deploy_does_not_write_to_a_state_root_the_user_never_established(
    tmp_path
):
    """deploy adopted state.game_dir as its write root with no question.

    Nothing named the directory: no flag, no config entry, no detection.
    """
    outside = tmp_path / "OUTSIDE"
    outside.mkdir()
    mods = tmp_path / "col"
    (mods / "ModA").mkdir(parents=True)
    (mods / "ModA" / "plugin.esp").write_bytes(b"esp bytes")
    _write_state(mods, game_dir=str(outside), deployed_files=[])

    with contextlib.suppress(Exception):
        _svc().deploy(mods)

    written = [p for p in outside.rglob("*") if p.is_file()]
    assert written == [], f"deploy wrote to a state-named root the user never established: {written}"
