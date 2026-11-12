"""Extraction staging tests: private per-call staging, races, cleanup.

Extraction must stage in a unique hidden directory on the destination
filesystem, never a shared predictable path: two concurrent extractions
of same-stem archives must produce correct disjoint results, and a
mid-extraction failure must leave no staging residue and no partial
content under the final names. No network, no real archives beyond
in-memory zips.
"""

import tempfile
import threading
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl import extractor
from nexus_collection_dl.extractor import ExtractionError, extract_archive


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    """Write a real zip archive at path with the given member payloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return path


def test_concurrent_same_stem_extractions_stay_disjoint(tmp_path, monkeypatch):
    """Two threads extract same-stem archives at the same time.

    The barrier holds both threads with their content fully staged,
    which is exactly where the old shared /tmp/nexus_extract_<stem> path
    let the second extraction recursively delete the first one's staged
    files. Unique private staging keeps both results correct.
    """
    archive_a = make_zip(
        tmp_path / "a" / "Mod-123-1.0.zip", {"Data/payload.txt": b"from A"}
    )
    archive_b = make_zip(
        tmp_path / "b" / "Mod-123-1.0.zip", {"Data/payload.txt": b"from B"}
    )
    target_a = tmp_path / "mods-a"
    target_b = tmp_path / "mods-b"

    barrier = threading.Barrier(2, timeout=30)
    real_extract_zip = extractor._extract_zip

    def staged_then_wait(archive_path, staging_dir):
        extracted = real_extract_zip(archive_path, staging_dir)
        barrier.wait()  # both extractions staged before either moves
        return extracted

    monkeypatch.setattr(extractor, "_extract_zip", staged_then_wait)

    results = {}
    errors = []

    def run(key, archive, target):
        try:
            results[key] = extract_archive(archive, target)
        except BaseException as e:  # surfaced by the assertions below
            errors.append((key, e))

    ta = threading.Thread(target=run, args=("a", archive_a, target_a))
    tb = threading.Thread(target=run, args=("b", archive_b, target_b))
    ta.start()
    tb.start()
    ta.join(timeout=60)
    tb.join(timeout=60)

    assert not ta.is_alive() and not tb.is_alive()
    assert errors == []
    # Each target received exactly its own archive's content
    assert (target_a / "Data" / "payload.txt").read_bytes() == b"from A"
    assert (target_b / "Data" / "payload.txt").read_bytes() == b"from B"
    assert results["a"] == [target_a / "Data" / "payload.txt"]
    assert results["b"] == [target_b / "Data" / "payload.txt"]
    # No staging residue in either destination
    assert not [p for p in target_a.iterdir() if p.name.startswith(".nexus-extract-")]
    assert not [p for p in target_b.iterdir() if p.name.startswith(".nexus-extract-")]


def test_staging_is_private_dir_on_destination_filesystem(tmp_path, monkeypatch):
    """Staging is a unique hidden mkdtemp directory (0700) inside the
    target directory: same filesystem as the final move, never shared."""
    archive = make_zip(tmp_path / "Mod-1.0.zip", {"f.txt": b"x"})
    target = tmp_path / "mods"
    seen = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        seen.append((kwargs, Path(path).stat().st_mode & 0o777))
        return path

    monkeypatch.setattr(extractor.tempfile, "mkdtemp", recording_mkdtemp)

    extract_archive(archive, target)

    assert len(seen) == 1
    kwargs, mode = seen[0]
    assert Path(kwargs["dir"]) == target        # on the destination filesystem
    assert kwargs["prefix"].startswith(".")     # hidden from dir scanners
    assert mode == 0o700                        # private to the user


def test_failure_mid_extraction_leaves_no_residue(tmp_path, monkeypatch):
    """A failure with content already staged publishes nothing under the
    final names and removes the staging directory completely."""
    archive = make_zip(
        tmp_path / "Mod-1.0.zip",
        {"Data/one.txt": b"one", "Data/two.txt": b"two"},
    )
    target = tmp_path / "mods"
    target.mkdir()

    def stage_one_then_fail(archive_path, staging_dir):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extract(zf.namelist()[0], staging_dir)
        raise RuntimeError("boom mid-extraction")

    monkeypatch.setattr(extractor, "_extract_zip", stage_one_then_fail)

    with pytest.raises(ExtractionError) as excinfo:
        extract_archive(archive, target)
    assert "boom mid-extraction" in str(excinfo.value)

    # No partial content under the final names and no staging debris
    assert list(target.iterdir()) == []


def test_unknown_archive_type_fails_before_staging(tmp_path):
    archive = tmp_path / "not-an-archive.bin"
    archive.write_bytes(b"plain text, not an archive")
    target = tmp_path / "mods"
    target.mkdir()

    with pytest.raises(ExtractionError, match="Unknown archive type"):
        extract_archive(archive, target)
    assert list(target.iterdir()) == []


def test_extracts_into_directory_with_special_characters(tmp_path):
    """Staging now lives under the destination, so mods dirs with spaces
    and non-ASCII characters must still extract correctly (system tools
    receive every path through argv lists, never a shell)."""
    archive = make_zip(tmp_path / "Mod-1.0.zip", {"Data/payload.txt": b"ok"})
    target = tmp_path / "Mod Folder ünïcode & spaces"

    files = extract_archive(archive, target)

    assert files == [target / "Data" / "payload.txt"]
    assert (target / "Data" / "payload.txt").read_bytes() == b"ok"
    assert not [p for p in target.iterdir() if p.name.startswith(".nexus-extract-")]
