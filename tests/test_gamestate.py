"""#115: installer conditions can see the game they are installing into.

`fileDependency` and `gameDependency` were undecidable at deploy time,
so multi-game installers like NVAC (which picks SKSE/FOSE/NVSE/OBSE
from the game version and the load order) fell back to the folder
heuristic with a per-mod warning. The warning made the gap visible; this
closes it.

The load order is read from the game's own plugins.txt in the Proton
prefix. That is what the game will see, so it is what the installer
should see.
"""

from __future__ import annotations

from pathlib import Path

from nexus_collection_dl.fomod_plan import (
    load_module_config_text,
    resolve_install_plan,
)

FILE_DEPENDENCY_CONFIG = """
<config>
  <moduleName>T</moduleName>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies>
          <fileDependency file="Master.esm" state="Active"/>
        </dependencies>
        <files><file source="skse/thing.esp" destination="thing.esp"/></files>
      </pattern>
      <pattern>
        <dependencies>
          <fileDependency file="Master.esm" state="Missing"/>
        </dependencies>
        <files><file source="other/thing.esp" destination="thing.esp"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""


def _write_plugins_txt(prefix: Path, game_domain: str, lines: list[str]) -> Path:
    from nexus_collection_dl.deploy import get_plugins_txt_dest

    dest = get_plugins_txt_dest(prefix, game_domain)
    assert dest is not None, game_domain
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n")
    return dest


def test_read_game_state_sees_active_plugins(tmp_path):
    """The load order the game will see is the one the installer sees."""
    from nexus_collection_dl.game_state import read_game_state

    _write_plugins_txt(
        tmp_path, "skyrimspecialedition", ["*Master.esm", "*Companion.esm"]
    )
    game = read_game_state(tmp_path, "skyrimspecialedition")
    assert game.plugin_states is not None
    assert game.plugin_state("Master.esm") == "Active"
    assert game.plugin_state("Companion.esm") == "Active"


def test_read_game_state_sees_inactive_plugins(tmp_path):
    """Listed but not starred: present and off. Not missing.

    A fileDependency on state="Inactive" is a real question and the
    answer is not "Missing".
    """
    from nexus_collection_dl.game_state import read_game_state

    _write_plugins_txt(tmp_path, "skyrimspecialedition", ["*Master.esm", "Off.esm"])
    game = read_game_state(tmp_path, "skyrimspecialedition")
    assert game.plugin_state("Off.esm") == "Inactive"


def test_read_game_state_reports_missing_plugins(tmp_path):
    """Not listed at all is Missing, which is what the condition means."""
    from nexus_collection_dl.game_state import read_game_state

    _write_plugins_txt(tmp_path, "skyrimspecialedition", ["*Master.esm"])
    game = read_game_state(tmp_path, "skyrimspecialedition")
    assert game.plugin_state("Nowhere.esm") == "Missing"


def test_no_plugins_txt_is_unknown_not_empty(tmp_path):
    """Absent load order means we do not know, not that nothing is active.

    Refusing to guess is what keeps a wrong branch from being picked.
    """
    from nexus_collection_dl.game_state import read_game_state

    game = read_game_state(tmp_path, "skyrimspecialedition")
    assert game.plugin_states is None
    assert game.plugin_state("Anything.esm") is None


def test_file_dependency_resolves_with_a_real_game_state(tmp_path):
    """The point: the condition decides instead of falling back."""
    _write_plugins_txt(tmp_path, "skyrimspecialedition", ["*Master.esm"])
    from nexus_collection_dl.game_state import read_game_state

    game = read_game_state(tmp_path, "skyrimspecialedition")
    config = load_module_config_text(FILE_DEPENDENCY_CONFIG)
    resolved = resolve_install_plan(config, {}, game=game)
    assert not resolved.indeterminate, resolved.notes
    sources = {e.source for e in resolved.entries}
    assert "skse/thing.esp" in sources, sources


def test_file_dependency_picks_the_other_branch_without_the_plugin(tmp_path):
    """The branch is chosen by what is actually installed."""
    _write_plugins_txt(tmp_path, "skyrimspecialedition", ["*Unrelated.esm"])
    from nexus_collection_dl.game_state import read_game_state

    game = read_game_state(tmp_path, "skyrimspecialedition")
    config = load_module_config_text(FILE_DEPENDENCY_CONFIG)
    resolved = resolve_install_plan(config, {}, game=game)
    assert not resolved.indeterminate, resolved.notes
    sources = {e.source for e in resolved.entries}
    assert "other/thing.esp" in sources, sources


def test_without_a_game_state_the_condition_is_still_indeterminate():
    """The gap this closes: no game state, no decision."""
    config = load_module_config_text(FILE_DEPENDENCY_CONFIG)
    resolved = resolve_install_plan(config, {})
    assert resolved.indeterminate, resolved
