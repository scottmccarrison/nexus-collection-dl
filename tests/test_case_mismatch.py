"""#134: case mismatches get surfaced before the game does.

Linux filesystems are case-sensitive. Mods are authored on
case-insensitive ones. A mod shipping ``Foo.esp`` where the load order
says ``foo.esp`` is two different files on ext4: the game reads one name
and the other is dead weight, or the load order points at a name that
does not exist.

We shipped this bug class ourselves (``plugins.txt`` vs ``Plugins.txt``,
fixed in 0.4.0). Limo ships a case-matching deployer for exactly this.

The rule: never silently rename. A rename the game does not expect is
its own bug. When the expectation is known (the manifest's load order),
fix up to it. When it is not, warn. Either way the user finds out here
rather than in game.
"""

from __future__ import annotations

from pathlib import Path

from nexus_collection_dl.deploy import (
    BackupStore,
    DeployJournal,
    DeploymentPlan,
)
from nexus_collection_dl.deploy import (
    deploy as run_deploy,
)


def _plan(staging: Path, entries: list[tuple[str, str]]) -> DeploymentPlan:
    pairs = []
    for src_rel, dest_rel in entries:
        src = staging / src_rel
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(src_rel.encode())
        pairs.append((src, Path(dest_rel)))
    return DeploymentPlan(data_files=pairs, source_root=staging)


def test_expected_plugin_case_wins_over_the_file(tmp_path):
    """The manifest knows how the game spells it. Use that.

    The file ships as Foo.esp; the load order says foo.esp. The deployed
    name has to be the one the load order will look for.
    """
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, [("Foo.esp", "Foo.esp")])

    result = run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
        expected_plugins=["foo.esp"],
    )
    assert result.errors == [], result.errors
    deployed = {Path(d.dest).name for d in result.deployed}
    assert deployed == {"foo.esp"}, deployed


def test_case_fixup_is_reported(tmp_path):
    """A rename is a thing that happened. It does not happen silently."""
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, [("Foo.esp", "Foo.esp")])

    result = run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
        expected_plugins=["foo.esp"],
    )
    assert any("case" in w.lower() or "foo.esp" in w for w in result.warnings), (
        result.warnings
    )


def test_case_collision_between_two_mods_is_warned(tmp_path):
    """Two mods shipping the same plugin with different case.

    On ext4 one of them is dead weight and nothing says which.
    """
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, [("a/Foo.esp", "Foo.esp"), ("b/foo.esp", "foo.esp")])

    result = run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
    )
    assert any("case" in w.lower() for w in result.warnings), result.warnings


def test_no_expectation_leaves_the_name_alone(tmp_path):
    """Without a known expectation, do not guess. Warn or say nothing.

    A silent rename is worse than an unchanged name: the game may expect
    the spelling the mod shipped.
    """
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, [("Foo.esp", "Foo.esp")])

    result = run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
    )
    deployed = {Path(d.dest).name for d in result.deployed}
    assert deployed == {"Foo.esp"}, deployed


def test_matching_case_is_not_renamed(tmp_path):
    """The common case must not change: no warning, no rename."""
    staging = tmp_path / "staging"
    game = tmp_path / "game"
    game.mkdir()
    plan = _plan(staging, [("foo.esp", "foo.esp")])

    result = run_deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
        expected_plugins=["foo.esp"],
    )
    deployed = {Path(d.dest).name for d in result.deployed}
    assert deployed == {"foo.esp"}
    assert result.warnings == [], result.warnings


# -- the service path ------------------------------------------------------
#
# The rename and its warning live in deploy(). The CLI and the shell go
# through ModManagerService.deploy, which used to return its own warning
# list and drop deploy()'s. The rename landed, the report did not.


def test_service_deploy_reports_the_case_fixup(tmp_path):
    """The rename must reach the user, not just the filesystem."""
    from nexus_collection_dl.service import ModManagerService
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "Foo.esp").write_bytes(b"PLUGIN")

    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Case Mod", "file_id": 11,
         "filename": "casemod.zip", "version": "1"}
    )
    state.mods[11].contributed_files = ["Foo.esp"]
    # The load order knows the plugin as foo.esp (lower case).
    state.manifest_data = {
        "plugins": [{"filename": "foo.esp", "name": "foo.esp"}],
        "mod_rules": [],
        "plugin_rules": [],
        "mod_phases": {},
        "logical_name_to_mod_id": {},
        "mod_choices": {},
    }
    state.save()

    game = tmp_path / "game"
    game.mkdir()
    svc = ModManagerService(api_key="test-key-unused")
    result = svc.deploy(mods, game_dir=game, use_copy=True)

    assert result.errors == [], result.errors
    # The rename happened...
    assert (game / "Data" / "foo.esp").is_file()
    assert not (game / "Data" / "Foo.esp").exists()
    # ...and it was reported.
    assert any("renamed" in w for w in result.warnings), result.warnings


def test_service_deploy_reports_a_case_collision(tmp_path):
    """Two names that differ only in case: the warning must reach the user."""
    from nexus_collection_dl.service import ModManagerService
    from nexus_collection_dl.state import CollectionState

    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "Foo.esp").write_bytes(b"ONE")
    (mods / "foo.esp").write_bytes(b"TWO")

    state = CollectionState(mods)
    state.set_collection_info("u", "n", 1, "skyrimspecialedition")
    state.add_mod(
        {"mod_id": 1, "mod_name": "Case Mod", "file_id": 11,
         "filename": "casemod.zip", "version": "1"}
    )
    state.mods[11].contributed_files = ["Foo.esp", "foo.esp"]
    state.save()

    game = tmp_path / "game"
    game.mkdir()
    svc = ModManagerService(api_key="test-key-unused")
    result = svc.deploy(mods, game_dir=game, use_copy=True)

    assert any("case-collides" in w or "case" in w.lower() for w in result.warnings), (
        result.warnings
    )
