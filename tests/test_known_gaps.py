"""Probes for the CHANGELOG's "Known gaps" claims.

One test per testable claim. Each asserts the gap is REAL: the test passes
while the gap exists and fails the moment it is closed. That is the point.
A claim that does not survive its probe is wrong, and the CHANGELOG line
goes. (This is how the overlap-guard claim was retired: the probe passed
against correct code, so the claim was false and got deleted.)

Claims with no test here and why:

- No field use (free-account downloads, Windows, the browser click chain
  end to end). Needs a real account and a real machine. Tracked in the
  field-verification checklist issue.
- Downgrading to 0.3.0 after a 0.4.0 deploy is untested. Needs a real
  downgrade run.

Already probed elsewhere and now false (the gap was closed):

- "The mods table does not surface stored FOMOD picks" (closed)
- "A deploy conflict names source paths, not the owning mods" (closed)
- "Installer configs have no size cap" (closed)
- "Archive extraction has no decompression size cap" (closed)
- "The overlap guard compares path strings rather than resolved paths"
  (never true; the guard has always resolved. See test_deploy_safety.py)
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from nexus_collection_dl.deploy import _fomod_plans
from nexus_collection_dl.extractor import find_fomod_installer
from nexus_collection_dl.redaction import boundary_redact
from nexus_collection_dl.state import CollectionState

DOMAIN = "skyrimspecialedition"

SIMPLE_CONFIG = """
<config>
  <moduleName>T</moduleName>
  <installSteps><installStep name="S1">
    <optionalFileGroups><group name="G" type="SelectExactlyOne">
      <plugins><plugin name="OptA">
        <files><file source="a/thing.esp" destination="thing.esp"/></files>
        <typeDescriptor><type name="Optional"/></typeDescriptor>
      </plugin></plugins>
    </group></optionalFileGroups>
  </installStep></installSteps>
</config>
"""

# A config whose install step is gated on a file being active. With no
# GameState there is no way to decide it.
FILE_DEPENDENCY_CONFIG = """
<config>
  <moduleName>T</moduleName>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies>
          <fileDependency file="Master.esm" state="Active"/>
        </dependencies>
        <files><file source="a/thing.esp" destination="thing.esp"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""


def _state_with(mods_dir: Path, **fields) -> CollectionState:
    mods_dir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(mods_dir)
    state.set_collection_info("u", "n", 1, DOMAIN)
    state.add_mod(
        {
            "mod_id": fields.get("mod_id", 1),
            "mod_name": fields.get("name", "M"),
            "file_id": fields.get("file_id", 11),
            "filename": fields.get("filename", "m.zip"),
            "version": fields.get("version", "1"),
        }
    )
    return state


# ---------------------------------------------------------------------------
# deploy has no confirmation
# ---------------------------------------------------------------------------


def test_known_gap_deploy_has_no_confirmation(tmp_path):
    """`deploy` runs to completion without asking, on purpose.

    CHANGELOG: the shell's /deploy is the only confirm and nothing is
    ever deployed automatically. A harness driving the CLI deploys
    non-interactively and that is the intended path.
    """
    from nexus_collection_dl.deploy import (
        BackupStore,
        DeployJournal,
        DeploymentPlan,
        deploy,
    )

    staging = tmp_path / "staging"
    (staging / "a").mkdir(parents=True)
    (staging / "a" / "thing.esp").write_bytes(b"x")
    game = tmp_path / "game"
    game.mkdir()

    plan = DeploymentPlan(
        data_files=[(staging / "a" / "thing.esp", Path("thing.esp"))],
        source_root=staging,
    )
    # No prompt, no interaction, no confirmation gate: it just runs.
    result = deploy(
        plan,
        game,
        method="copy",
        dry_run=False,
        backups=BackupStore(game),
        journal=DeployJournal(game),
        ownership={},
    )
    assert result.errors == []
    assert len(result.deployed) == 1
    assert (game / "Data" / "thing.esp").exists()


# ---------------------------------------------------------------------------
# fileDependency / gameDependency fall back
# ---------------------------------------------------------------------------


def test_known_gap_file_dependency_is_decidable_with_a_game_state(tmp_path):
    """The gap was "deploy cannot see the game's load order". Closed.

    The probe used to assert the fallback: a fileDependency with no
    GameState is undecidable. That is still true in isolation, and it is
    the right behaviour. What is closed is the wiring: deploy now builds
    a GameState from the game's own plugins.txt, so the condition
    decides instead of falling back. See test_gamestate.py.
    """
    from nexus_collection_dl.fomod_plan import (
        GameState,
        load_module_config_text,
        resolve_install_plan,
    )

    config = load_module_config_text(FILE_DEPENDENCY_CONFIG)
    assert config is not None

    # Without a game state, still undecidable. Refusing to guess is the
    # contract, not the gap.
    resolved = resolve_install_plan(config, {})
    assert resolved.indeterminate

    # With one, the condition decides.
    resolved = resolve_install_plan(
        config, {}, game=GameState(plugin_states={"master.esm": "Active"})
    )
    assert not resolved.indeterminate, resolved.notes


# ---------------------------------------------------------------------------
# Full installer XML lives in the state file
# ---------------------------------------------------------------------------


def test_installer_xml_stored_in_sidecar_not_inline(tmp_path):
    """The ModuleConfig.xml goes to a sidecar file, not the state file.

    The state stores only the member path and a file reference; the
    full XML lives in .nexus-fomod/<file_id>.xml.
    """
    mods = tmp_path / "mods"
    state = _state_with(mods)
    state.mods[11].fomod_config = {
        "member": "fomod/ModuleConfig.xml",
        "xml": SIMPLE_CONFIG,
    }
    state.save()

    data = json.loads((mods / CollectionState(mods).state_file.name).read_text())
    cfg = data["mods"]["11"]["fomod_config"]
    assert "xml" not in cfg, "full XML must not be inline in the state file"
    assert cfg["member"] == "fomod/ModuleConfig.xml"
    assert "xml_file" in cfg, "a sidecar reference must be present"

    # The sidecar file carries the full XML
    sidecar = mods / cfg["xml_file"]
    assert sidecar.is_file(), f"sidecar missing: {sidecar}"
    assert sidecar.read_text() == SIMPLE_CONFIG


# ---------------------------------------------------------------------------
# System-tool extraction fallbacks do not pre-validate member names
# ---------------------------------------------------------------------------


def test_system_tool_extraction_pre_lists_members_for_validation(
    tmp_path, monkeypatch
):
    """The `7z` fallback lists members before extracting to validate names.

    The library paths validate names before extracting; the subprocess
    fallbacks now do too (7z l -ba runs first and each member is checked
    before 7z x writes anything).
    """
    from nexus_collection_dl import extractor

    archive = tmp_path / "a.7z"
    archive.write_bytes(b"7z\xbc\xaf'\x1c" + b"\x00" * 32)

    calls: list[list[str]] = []

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))

        class R:
            returncode = 0
            stderr = ""
            stdout = ""

        return R()

    monkeypatch.setattr(extractor.subprocess, "run", spy)
    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/bin/7z")

    target = tmp_path / "out"
    target.mkdir()
    monkeypatch.setattr(
        extractor,
        "py7zr",
        type(
            "M",
            (),
            {
                "SevenZipFile": None,
                "UnsupportedCompressionMethodError": type(
                    "U", (Exception,), {}
                ),
                "Bad7zFile": type("B", (Exception,), {}),
            },
        ),
    )
    try:
        extractor._extract_7z_system(archive, target)
    except Exception:  # noqa: S110 - the call is what is under test, not its result
        pass

    assert calls, "the system tool should have been invoked"
    # First call lists members for pre-validation, second extracts.
    assert calls[0][1] == "l", f"expected listing first, got {calls[0]}"
    assert calls[1][1] == "x", f"expected extraction second, got {calls[1]}"


# ---------------------------------------------------------------------------
# Redaction has no diagnostic escape
# ---------------------------------------------------------------------------


def test_known_gap_redaction_is_one_way():
    """The gap was "no diagnostic escape". Closed: there is one now.

    The probe used to assert the redaction was one-way with no way for a
    maintainer to recover the detail. It flips when the gap closes: the
    default is still safe, and `diagnostic=True` is the escape hatch.
    """
    import inspect


    secret = "sk-or-v1-abcdef0123456789abcdef0123456789"
    text = f"failed with key {secret} in the request"

    out = boundary_redact(text, (secret,))
    assert secret not in out
    assert "[redacted]" in out

    sig = inspect.signature(boundary_redact)
    assert "diagnostic" in sig.parameters, list(sig.parameters)

    diag = boundary_redact(text, (secret,), diagnostic=True)
    assert secret in diag


# ---------------------------------------------------------------------------
# Removed `serve` reports "command not found"
# ---------------------------------------------------------------------------


def test_known_gap_removed_serve_reports_a_replacement():
    """The gap was "reports command not found". Closed: it says what to use.

    The probe used to assert the bare argparse error. It flips when the
    gap closes: the stub names the replacement and still exits non-zero.
    """
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    r = CliRunner().invoke(cli_module.main, ["serve"])
    assert r.exit_code != 0
    combined = (r.output + str(r.exception)).lower()
    assert "shell" in combined
    assert "no such command" not in combined


# ---------------------------------------------------------------------------
# Legacy tree scan and archive marker check disagree on ModuleConfig depth
# ---------------------------------------------------------------------------


def test_known_gap_moduleconfig_depth_rules_agree(tmp_path):
    """The two rules were one gap. Closed: they agree now.

    The CHANGELOG claim was "the legacy tree scan and the archive marker
    check disagree on how deep a ModuleConfig.xml may live". This is the
    probe for it, flipped: the gap is closed and the test asserts the
    agreement rather than the disagreement. See
    test_fomod_marker_rule.py for the shared rule.
    """

    # A config with no `fomod` segment: `SomeMod/ModuleConfig.xml`.
    # Deliberately not a marker under the shared rule, and both callers
    # say so.
    staging = tmp_path / "staging"
    (staging / "SomeMod").mkdir(parents=True)
    (staging / "SomeMod" / "ModuleConfig.xml").write_text(SIMPLE_CONFIG)
    (staging / "SomeMod" / "a").mkdir()
    (staging / "SomeMod" / "a" / "thing.esp").write_bytes(b"x")

    plans, _warnings = _fomod_plans(staging, {})
    assert not plans, "the tree scan should not plan a non-marker config"

    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SomeMod/ModuleConfig.xml", SIMPLE_CONFIG)
        zf.writestr("SomeMod/a/thing.esp", b"x")
    assert find_fomod_installer(archive) is None

    # And the layout that IS a marker agrees the other way too.
    (staging / "fomod").mkdir()
    (staging / "fomod" / "ModuleConfig.xml").write_text(SIMPLE_CONFIG)
    plans, _ = _fomod_plans(staging, {})
    assert plans, "the tree scan should plan fomod/ModuleConfig.xml"


# ---------------------------------------------------------------------------
# 0.3.0 to 0.4.0 migration matches options by name
# ---------------------------------------------------------------------------


def test_known_gap_migration_matches_options_by_name_with_a_warning(tmp_path):
    """A pre-0.4.0 installer with no option records matches by name.

    CHANGELOG: installers extracted before this release carry no option
    records, so deploy matches their options by name from stored choices
    and warns that it is doing so.
    """
    staging = tmp_path / "staging"
    (staging / "fomod").mkdir(parents=True)
    (staging / "fomod" / "ModuleConfig.xml").write_text(SIMPLE_CONFIG)
    (staging / "a").mkdir()
    (staging / "a" / "thing.esp").write_bytes(b"x")

    # Picks recorded for a different file_id, standing in for choices
    # stored before the installer config was captured.
    plans, warnings = _fomod_plans(
        staging,
        {},
        fomod_picks={99: [{"step": "S1", "group": "G", "option": "OptA"}]},
        fomod_configs=None,
    )
    assert plans
    assert any("matched by name" in w for w in warnings), warnings
