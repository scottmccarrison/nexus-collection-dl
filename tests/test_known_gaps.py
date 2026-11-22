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
import re
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


def test_known_gap_file_dependency_falls_back_with_a_warning(tmp_path):
    """An undecidable installer condition falls back to the folder install.

    CHANGELOG: fileDependency and gameDependency conditions fall back to
    the folder-based install with a per-mod warning until deploy can see
    the game's load order.
    """
    from nexus_collection_dl.fomod_plan import (
        load_module_config_text,
        resolve_install_plan,
    )

    config = load_module_config_text(FILE_DEPENDENCY_CONFIG)
    assert config is not None

    resolved = resolve_install_plan(config, {})
    assert resolved.indeterminate, (
        "a fileDependency condition with no GameState should be undecidable, "
        f"got {resolved}"
    )
    assert resolved.notes, "the fallback must say why it could not decide"


# ---------------------------------------------------------------------------
# Full installer XML lives in the state file
# ---------------------------------------------------------------------------


def test_known_gap_full_installer_xml_stored_in_state(tmp_path):
    """The whole ModuleConfig.xml is serialized into .nexus-state.json.

    CHANGELOG: a few KB to about 60KB per FOMOD mod, rewritten on each
    save.
    """
    mods = tmp_path / "mods"
    state = _state_with(mods)
    state.mods[11].fomod_config = {
        "member": "fomod/ModuleConfig.xml",
        "xml": SIMPLE_CONFIG,
    }
    state.save()

    data = json.loads((mods / CollectionState(mods).state_file.name).read_text())
    stored = data["mods"]["11"]["fomod_config"]["xml"]
    assert stored == SIMPLE_CONFIG
    assert len(stored) > 100


# ---------------------------------------------------------------------------
# System-tool extraction fallbacks do not pre-validate member names
# ---------------------------------------------------------------------------


def test_known_gap_system_tool_extraction_does_not_pre_list_members(
    tmp_path, monkeypatch
):
    """The `7z` fallback extracts without listing members first.

    CHANGELOG: name pre-validation for the system-tool extraction
    fallbacks. The library paths validate names before extracting; the
    subprocess fallbacks do not, and rely on the after-the-fact snapshot
    guard instead.
    """
    from nexus_collection_dl import extractor

    archive = tmp_path / "a.7z"
    archive.write_bytes(b"7z\xbc\xaf'\x1c" + b"\x00" * 32)

    calls: list[list[str]] = []

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        # Answer the extraction call as if 7z succeeded and wrote nothing.
        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(extractor.subprocess, "run", spy)
    monkeypatch.setattr(extractor.shutil, "which", lambda name: "/usr/bin/7z")

    target = tmp_path / "out"
    target.mkdir()
    # Force the system-tool path by making py7zr refuse the archive.
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

    # Every invocation is `7z x ...`. Nothing asks `7z l -slt` for the
    # member list first, which is what pre-validation would need.
    assert calls, "the system tool should have been invoked"
    for cmd in calls:
        assert cmd[1] == "x", f"expected extraction, got {cmd}"
        assert "l" not in cmd and "-slt" not in cmd


# ---------------------------------------------------------------------------
# Redaction has no diagnostic escape
# ---------------------------------------------------------------------------


def test_known_gap_redaction_is_one_way(tmp_path):
    """Scrubbed text cannot be recovered, and there is no escape hatch.

    CHANGELOG: error-text scrubbing has no diagnostic escape for a
    maintainer reading a bug report.
    """
    import inspect

    secret = "sk-or-v1-abcdef0123456789abcdef0123456789"
    text = f"failed with key {secret} in the request"

    out = boundary_redact(text, (secret,))
    assert secret not in out
    assert "[redacted]" in out

    # No parameter invites an unredacted or diagnostic view.
    for name in inspect.signature(boundary_redact).parameters:
        assert not re.search(r"diagnos|escape|unredact|verbose|debug", name, re.IGNORECASE), name

    # And the module exposes no companion that would hand the original back.
    import nexus_collection_dl.redaction as redaction_module

    public = [n for n in dir(redaction_module) if not n.startswith("_")]
    assert not [
        n for n in public if re.search(r"diagnos|unredact|reveal", n, re.IGNORECASE)
    ], public


# ---------------------------------------------------------------------------
# Removed `serve` reports "command not found"
# ---------------------------------------------------------------------------


def test_known_gap_removed_serve_reports_command_not_found(tmp_path):
    """`nexus-dl serve` is a bare "no such command", not a deprecation stub.

    CHANGELOG: the removed `serve` command reports "command not found"
    rather than a deprecation stub.
    """
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module

    r = CliRunner().invoke(cli_module.main, ["serve"])
    assert r.exit_code != 0
    combined = (r.output + str(r.exception)).lower()
    assert "no such command" in combined or "usage" in combined
    # A deprecation stub would name the replacement. This does not.
    assert "shell" not in combined


# ---------------------------------------------------------------------------
# Legacy tree scan and archive marker check disagree on ModuleConfig depth
# ---------------------------------------------------------------------------


def test_known_gap_moduleconfig_depth_rules_disagree(tmp_path):
    """Two code paths, two answers, for the same archive.

    CHANGELOG: the legacy tree scan and the archive marker check disagree
    on how deep a ModuleConfig.xml may live. The extractor requires a
    `fomod` path segment; the deploy tree scan takes any basename within
    three path parts.
    """
    # A config with no `fomod` segment: `SomeMod/ModuleConfig.xml`.
    staging = tmp_path / "staging"
    (staging / "SomeMod").mkdir(parents=True)
    (staging / "SomeMod" / "ModuleConfig.xml").write_text(SIMPLE_CONFIG)
    (staging / "SomeMod" / "a").mkdir()
    (staging / "SomeMod" / "a" / "thing.esp").write_bytes(b"x")

    # The deploy tree scan finds it and builds a plan.
    plans, _warnings = _fomod_plans(staging, {})
    assert plans, "the deploy tree scan should pick up SomeMod/ModuleConfig.xml"

    # The extractor's marker check, given the same layout in an archive,
    # does not: there is no `fomod` segment.
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SomeMod/ModuleConfig.xml", SIMPLE_CONFIG)
        zf.writestr("SomeMod/a/thing.esp", b"x")
    assert find_fomod_installer(archive) is None, (
        "the marker check requires a `fomod` segment and should not "
        "recognise SomeMod/ModuleConfig.xml"
    )


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
