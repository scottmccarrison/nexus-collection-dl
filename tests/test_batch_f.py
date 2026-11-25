"""Batch F: extraction bugs from the 0.5.0 gate.

#122: tree scan and marker check must agree on ModuleConfig.xml depth.
#123: exotic installers (script.cs, info.xml-only) must be detected.
#116: system-tool extraction fallbacks must pre-validate member names.
"""
from __future__ import annotations

import zipfile

import pytest

# ---------------------------------------------------------------------------
# #122: both paths agree on ModuleConfig.xml depth
# ---------------------------------------------------------------------------


def test_marker_check_and_tree_scan_agree_on_wrapper_depth(tmp_path):
    """SomeMod/ModuleConfig.xml (no fomod/) must be treated the same by both."""
    from nexus_collection_dl.extractor import _fomod_marker_from_name

    # At any depth, a wrapper dir with no fomod/ is not a marker
    assert _fomod_marker_from_name("SomeMod/ModuleConfig.xml") is None
    assert _fomod_marker_from_name("a/b/c/ModuleConfig.xml") is None

    # Under fomod/, it is
    assert _fomod_marker_from_name("fomod/ModuleConfig.xml") is not None
    assert _fomod_marker_from_name("SomeMod/fomod/ModuleConfig.xml") is not None

    # At root, header names are
    assert _fomod_marker_from_name("ModuleConfig.xml") is not None
    assert _fomod_marker_from_name("info.xml") is not None

    # Script names at root are not
    assert _fomod_marker_from_name("script.cs") is None
    assert _fomod_marker_from_name("script.lua") is None


# ---------------------------------------------------------------------------
# #123: exotic installers are detected as FOMOD
# ---------------------------------------------------------------------------


def test_script_cs_under_fomod_is_detected(tmp_path):
    from nexus_collection_dl.extractor import find_fomod_installer
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fomod/script.cs", "// installer")
        zf.writestr("Data/m.esp", "x")
    assert find_fomod_installer(archive) is not None


def test_info_xml_only_under_fomod_is_detected(tmp_path):
    from nexus_collection_dl.extractor import find_fomod_installer
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("fomod/info.xml", "<plugin/>")
        zf.writestr("Data/m.esp", "x")
    assert find_fomod_installer(archive) is not None


def test_plain_mod_is_not_mistaken_for_fomod(tmp_path):
    from nexus_collection_dl.extractor import find_fomod_installer
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Data/m.esp", "x")
        zf.writestr("readme.txt", "hello")
    assert find_fomod_installer(archive) is None


# ---------------------------------------------------------------------------
# #116: system-tool extraction fallbacks pre-validate member names
# ---------------------------------------------------------------------------


def test_7z_system_prevalidates_member_names(monkeypatch, tmp_path):
    """A malicious member name must be refused before extraction."""
    from unittest.mock import MagicMock, patch

    from nexus_collection_dl import extractor

    archive = tmp_path / "evil.7z"
    archive.write_bytes(b"fake")

    # Mock: 7z l lists a traversal member, 7z x would succeed
    def fake_run(cmd, **kw):
        r = MagicMock()
        if "l" in cmd:
            r.returncode = 0
            # 7z l -ba format: date time attr size compressed name
            r.stdout = "2024-01-01 00:00:00 ....A 10 10 ../evil.txt\n"
        else:
            r.returncode = 0
        return r

    with patch("subprocess.run", side_effect=fake_run), pytest.raises(extractor.ExtractionError, match=r"traversal|\.\.|outside"):
            extractor._extract_7z_system(archive, tmp_path / "out")
