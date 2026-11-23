"""One FOMOD marker rule, used by every caller.

Two paths used to answer "is this a FOMOD installer?" differently:

- ``extractor.find_fomod_installer`` required the parent directory to be
  ``fomod``.
- ``deploy._fomod_plans`` took any ``ModuleConfig.xml`` basename within
  three path parts.

The same archive could be a FOMOD mod to one and a plain mod to the
other, which is how a user gets behavior that depends on which command
they ran. This pins the single shared rule.

The rule, after this and the exotic-installer widening:

- The basename is one of ``ModuleConfig.xml``, ``script.lua``,
  ``script.cs``, or ``info.xml`` (any case).
- And either the parent directory is ``fomod`` (any case), or the file
  sits at the archive root (one segment). A shallow ``info.xml`` with no
  ``fomod`` in sight is the generic-name risk, which is why it is only
  accepted at the root and everywhere a ``fomod`` parent vouches for it.

``SomeMod/ModuleConfig.xml`` (no ``fomod`` directory, one wrapper deep)
is deliberately NOT a marker. It used to be planned by the tree scan and
not detected by the marker check; the two agree on "not a marker" now.
Missing an exotic installer is the cheaper error of the two: a false
"this is a FOMOD" refuses a normal mod, while a false "this is not"
falls back to the folder heuristic.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from nexus_collection_dl.deploy import _fomod_plans
from nexus_collection_dl.extractor import (
    _fomod_marker_from_name,
    find_fomod_installer,
)


def _zip(tmp_path: Path, members: list[str]) -> Path:
    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name in members:
            zf.writestr(name, b"<config/>")
    return archive


# ---------------------------------------------------------------------------
# #122: the two paths agree
# ---------------------------------------------------------------------------

# (members, is_a_marker) for the layouts worth pinning. The point of each
# row is the agreement, not the verdict: whichever way it goes, both
# callers must give the same answer.
LAYOUTS = [
    (["fomod/ModuleConfig.xml"], True),
    (["Some Mod/fomod/ModuleConfig.xml"], True),
    (["FOMOD/Info.XML"], True),
    (["fomod/script.lua"], True),
    (["ModuleConfig.xml"], True),
    (["SomeMod/ModuleConfig.xml"], False),
    (["a/b/c/ModuleConfig.xml"], False),
    (["textures/thing.dds"], False),
]


def test_marker_rule_is_one_function():
    """There is exactly one rule to change, and both callers use it."""
    for members, expected in LAYOUTS:
        name = members[0]
        got = _fomod_marker_from_name(name) is not None
        assert got is expected, (name, got, expected)


def test_archive_detection_and_tree_scan_agree(tmp_path):
    """The disagreement, closed.

    For every layout, find_fomod_installer and the deploy tree scan must
    say the same thing about whether there is a FOMOD installer here.
    This is the test the known-gaps probe used to fail against.
    """
    for members, expected in LAYOUTS:
        case = tmp_path / members[0].replace("/", "_")
        case.mkdir(exist_ok=True)
        archive = _zip(case, members)

        detected = find_fomod_installer(archive) is not None

        # The tree scan works on an extracted tree. Build one that
        # matches the archive layout and ask the deploy side.
        tree = tmp_path / "tree" / members[0].replace("/", "_")
        for name in members:
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<config/>")
        plans, _warnings = _fomod_plans(tree, {})
        planned = bool(plans)

        assert detected == planned, (
            f"{members[0]}: archive detection said {detected}, "
            f"tree scan said {planned}"
        )
        assert detected == expected, (members[0], detected, expected)


# ---------------------------------------------------------------------------
# #123: exotic installers
# ---------------------------------------------------------------------------


def test_script_cs_is_a_fomod_marker():
    """A C# script installer is still an installer.

    It used to slip past detection and every option folder deployed.
    """
    assert _fomod_marker_from_name("fomod/script.cs") is not None
    assert _fomod_marker_from_name("Some Mod/fomod/Script.CS") is not None


def test_top_level_moduleconfig_is_a_fomod_marker():
    """An installer at the archive root, with no fomod directory.

    Real mods ship this. It is the layout the tree scan used to catch
    and the marker check used to miss.
    """
    assert _fomod_marker_from_name("ModuleConfig.xml") is not None


def test_top_level_info_xml_is_a_fomod_marker():
    """info.xml at the root is a FOMOD header; deeper it is a generic name."""
    assert _fomod_marker_from_name("info.xml") is not None
    assert _fomod_marker_from_name("FOMOD/Info.XML") is not None
    # One wrapper deep with no fomod vouching for it: too generic to call.
    assert _fomod_marker_from_name("SomeMod/info.xml") is None


def test_deeply_nested_unrelated_names_are_not_markers():
    """The rule must not light up on every file called ModuleConfig.xml."""
    assert _fomod_marker_from_name("a/b/c/d/ModuleConfig.xml") is None
    assert _fomod_marker_from_name("textures/script.lua") is None
    assert _fomod_marker_from_name("script.cs") is None
