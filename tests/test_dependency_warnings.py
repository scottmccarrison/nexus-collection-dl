"""#47: `add` warns when a mod's requirements are not installed.

Collections handle dependencies implicitly (the author includes them).
`nexus-dl add` does not: it takes one mod and has no idea what else that
mod needs. Vortex warns. We did not.

Warn, never block: a missing dependency is a thing the user may know
about and have decided against. The warning is the whole feature.

Where the requirement list comes from is the honest part. The Nexus
GraphQL collection query carries `modRequirements`; a standalone mod's
requirements come from whatever the mod info gives us. When there is no
list to check, there is no warning, because we cannot check what we
cannot see. Silence is the right failure mode: a false warning is worse
than none.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module
from nexus_collection_dl.state import CollectionState


def _state_with_mods(tmp_path: Path, mods: list[dict]) -> CollectionState:
    root = tmp_path / "mods"
    root.mkdir(parents=True, exist_ok=True)
    state = CollectionState(root)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    for i, spec in enumerate(mods):
        state.add_mod(
            {
                "mod_id": spec["mod_id"],
                "mod_name": spec.get("name", f"Mod {spec['mod_id']}"),
                "file_id": spec.get("file_id", 100 + i),
                "filename": spec.get("filename", "m.zip"),
                "version": "1",
            }
        )
        if "requirements" in spec:
            state.mods[spec.get("file_id", 100 + i)].requirements = spec["requirements"]
    return state


def test_missing_requirements_are_reported(tmp_path):
    """The check itself: which required mod_ids are not present."""
    from nexus_collection_dl.service import missing_requirements

    state = _state_with_mods(
        tmp_path, [{"mod_id": 1, "requirements": [2, 3]}, {"mod_id": 2}]
    )
    missing = missing_requirements(state, [2, 3, 4])
    assert missing == [3, 4]


def test_all_requirements_present_reports_nothing(tmp_path):
    from nexus_collection_dl.service import missing_requirements

    state = _state_with_mods(tmp_path, [{"mod_id": 1, "requirements": [2]}, {"mod_id": 2}])
    assert missing_requirements(state, [2]) == []


def test_no_requirements_means_nothing_to_check(tmp_path):
    """An empty list is "nothing known", not "nothing needed".

    Either way there is nothing to warn about, and the caller must not
    invent a warning from an empty list.
    """
    from nexus_collection_dl.service import missing_requirements

    state = _state_with_mods(tmp_path, [{"mod_id": 1}])
    assert missing_requirements(state, []) == []
    assert missing_requirements(state, None) == []


def test_add_warns_on_missing_requirements(tmp_path, monkeypatch):
    """The point of the feature: the user is told before they find out
    in game.
    """
    from nexus_collection_dl import service as service_module

    state = _state_with_mods(tmp_path, [{"mod_id": 1}])
    state.save()

    monkeypatch.setattr(
        service_module.ModManagerService,
        "add_mod",
        lambda self, *a, **k: service_module.AddResult(
            mod_name="Needs A Lot",
            mod_id=9,
            file_name="m.zip",
            success=True,
            warnings=["missing dependency: Some Required Mod (mod 42)"],
        ),
    )
    r = CliRunner().invoke(
        cli_module.main, ["add", "https://www.nexusmods.com/skyrimspecialedition/mods/9", str(state.mods_dir)]
    )
    assert r.exit_code == 0, r.output
    assert "missing dependency" in r.output.lower()


def test_add_warning_does_not_block(tmp_path, monkeypatch):
    """Warn, never block. The mod installs anyway."""
    from nexus_collection_dl import service as service_module

    state = _state_with_mods(tmp_path, [{"mod_id": 1}])
    state.save()

    monkeypatch.setattr(
        service_module.ModManagerService,
        "add_mod",
        lambda self, *a, **k: service_module.AddResult(
            mod_name="Needs A Lot",
            mod_id=9,
            file_name="m.zip",
            success=True,
            warnings=["missing dependency: X"],
        ),
    )
    r = CliRunner().invoke(
        cli_module.main, ["add", "https://www.nexusmods.com/skyrimspecialedition/mods/9", str(state.mods_dir)]
    )
    assert r.exit_code == 0
