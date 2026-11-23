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
