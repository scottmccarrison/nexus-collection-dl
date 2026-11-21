"""Archive members must never escape staging or plant tool-owned files.

The extractor's job is third-party archives: member names are untrusted
input, and the system-tool fallbacks (``7z x``, ``unar``, ``unrar x``)
recreate symlinks. These pin the refusal at extract time and the
staging-tree check that runs before anything is merged.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl import extractor
from nexus_collection_dl.extractor import ExtractionError, extract_archive


def _make_zip(tmp_path: Path, members: dict[str, bytes], name: str = "mod.zip") -> Path:
    archive = tmp_path / name
    with zipfile.ZipFile(archive, "w") as zf:
        for member, payload in members.items():
            zf.writestr(member, payload)
    return archive


def _make_zip_with_symlink(tmp_path: Path, link_name: str, target: str) -> Path:
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo(link_name)
        info.create_system = 3  # Unix
        info.external_attr = 0o120777 << 16
        zf.writestr(info, target)
    return archive


def test_zip_member_with_parent_traversal_is_refused(tmp_path):
    archive = _make_zip(tmp_path, {"../evil.txt": b"gotcha"})
    with pytest.raises(ExtractionError):
        extract_archive(archive, tmp_path / "mods")
    assert not list(tmp_path.rglob("evil.txt"))


def test_zip_member_with_absolute_path_is_refused(tmp_path):
    archive = _make_zip(tmp_path, {"/tmp/evil.txt": b"gotcha"})
    with pytest.raises(ExtractionError):
        extract_archive(archive, tmp_path / "mods")
    assert not (tmp_path / "mods" / "tmp").exists()


def test_zip_symlink_member_is_refused(tmp_path):
    archive = _make_zip_with_symlink(tmp_path, "evil", "/etc/passwd")
    with pytest.raises(ExtractionError):
        extract_archive(archive, tmp_path / "mods")


def test_zip_backslash_member_becomes_directories(tmp_path):
    archive = _make_zip(tmp_path, {"Data\\foo.esp": b"x"})
    extract_archive(archive, tmp_path / "mods")
    assert (tmp_path / "mods" / "Data" / "foo.esp").exists()
    assert not (tmp_path / "mods" / "Data\\foo.esp").exists()


def test_archive_cannot_plant_tool_owned_files(tmp_path):
    for member in (
        ".nexus-state.json",
        ".nexus-journal.jsonl",
        ".nexus-backups/012345",
        ".NEXUS-STATE.JSON",
    ):
        archive = _make_zip(tmp_path, {member: b"forged", "ok.txt": b"x"})
        with pytest.raises(ExtractionError):
            extract_archive(archive, tmp_path / "mods")
        assert not list((tmp_path / "mods").rglob("*nexus*"))


def test_extract_archive_rejects_a_planted_symlink(tmp_path, monkeypatch):
    """The walk covers what the system-tool fallbacks write.

    ``7z x`` and friends recreate symlinks during extraction; by the
    time anything runs a name check the link already exists, so the
    staging tree is walked before the merge.
    """
    archive = _make_zip(tmp_path, {"ok.txt": b"x"})
    mods = tmp_path / "mods"

    def plant(archive_path: Path, target_dir: Path) -> list[Path]:
        (target_dir / "ok.txt").write_bytes(b"x")
        os.symlink("/etc/passwd", target_dir / "evil")
        return []

    monkeypatch.setattr("nexus_collection_dl.extractor._extract_zip", plant)
    with pytest.raises(ExtractionError):
        extract_archive(archive, mods)
    assert not (mods / "evil").exists()


def test_extract_archive_rejects_a_planted_fifo(tmp_path, monkeypatch):
    archive = _make_zip(tmp_path, {"ok.txt": b"x"})
    mods = tmp_path / "mods"

    def plant(archive_path: Path, target_dir: Path) -> list[Path]:
        os.mkfifo(target_dir / "pipe")
        return []

    monkeypatch.setattr("nexus_collection_dl.extractor._extract_zip", plant)
    with pytest.raises(ExtractionError):
        extract_archive(archive, mods)


def test_validate_extracted_tree_rejects_symlink_and_fifo(tmp_path):
    """Direct pin on the staging-tree walk itself.

    Rule 2 of the hardening: the walk that runs before the merge must
    reject symlink entries and non-regular files (fifo/device/socket),
    naming the offending relative path. This is the contract the
    system-tool fallbacks are held to; the planted-entry tests above
    cover the same walk through ``extract_archive``.
    """
    link_dir = tmp_path / "link-staging"
    link_dir.mkdir()
    os.symlink("/etc/passwd", link_dir / "evil")
    with pytest.raises(ExtractionError, match=r"archive contains a symlink \(evil\)"):
        extractor._validate_extracted_tree(link_dir)

    fifo_dir = tmp_path / "fifo-staging"
    fifo_dir.mkdir()
    os.mkfifo(fifo_dir / "pipe")
    with pytest.raises(
        ExtractionError, match=r"archive contains a non-regular file \(pipe\)"
    ):
        extractor._validate_extracted_tree(fifo_dir)


def test_normal_archive_still_extracts(tmp_path):
    """Hardening must not reject a legitimate mod."""
    archive = _make_zip(
        tmp_path,
        {
            "Cool Mod/fomod/ModuleConfig.xml": b"<config/>",
            "Cool Mod/Data/thing.esp": b"x",
        },
    )
    moved = extract_archive(archive, tmp_path / "mods")
    assert sorted(
        p.relative_to(tmp_path / "mods").as_posix() for p in moved
    ) == [
        "Cool Mod/Data/thing.esp",
        "Cool Mod/fomod/ModuleConfig.xml",
    ]


def test_archive_cannot_plant_tool_owned_files_below_the_root(tmp_path):
    """A tool-owned name is reserved at any depth, not just the root.

    scan_collections treats any directory holding .nexus-state.json as a
    collection, so one planted below the root is just as loadable as one
    at it.
    """
    archive = _make_zip(tmp_path, {"SomeMod/.nexus-state.json": b"forged"})
    with pytest.raises(ExtractionError):
        extract_archive(archive, tmp_path / "mods")


def test_zip_symlink_member_is_refused_from_any_creator(tmp_path):
    """Symlink mode in the external attributes is enough to refuse."""
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("evil")
        info.create_system = 0
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ExtractionError):
        extract_archive(archive, tmp_path / "mods")


def test_file_cannot_nest_inside_an_existing_directory(tmp_path):
    """shutil.move would silently nest and report the wrong path."""
    mods = tmp_path / "mods"
    (mods / "Foo").mkdir(parents=True)
    (mods / "Foo" / "keep.txt").write_text("k")
    archive = _make_zip(tmp_path, {"Foo": b"file named like a directory"})
    with pytest.raises(ExtractionError):
        extract_archive(archive, mods)


def test_extract_archive_rejects_writes_outside_staging(tmp_path, monkeypatch):
    """Anything a tool drops beside staging is removed and refused.

    The system-tool fallbacks take untrusted names; a tool that does not
    strip `..` writes into the collection tree during extraction, before
    any name check could run.
    """
    archive = _make_zip(tmp_path, {"ok.txt": b"x"})
    mods = tmp_path / "mods"

    def plant(archive_path: Path, staging: Path) -> list[Path]:
        (staging.parent / "planted.txt").write_bytes(b"pwned")
        (staging / "ok.txt").write_bytes(b"x")
        return []

    monkeypatch.setattr("nexus_collection_dl.extractor._extract_zip", plant)
    with pytest.raises(ExtractionError):
        extract_archive(archive, mods)
    assert not (mods / "planted.txt").exists()
