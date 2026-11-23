"""#121: extraction publishes as one unit, or says it did not.

`_move_staging_contents` moved files one at a time and the `finally`
block deleted staging either way. A failure halfway left a partial set
in the target with no record that the publish was incomplete and
nothing left to repair from.

The contract now:

- A marker records the in-flight publish before the first move.
- It is removed on success.
- On failure the marker and the staging dir both stay, so the next run
  can finish what this one started instead of guessing.
- The next extract into the same target sees the marker and repairs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nexus_collection_dl.extractor import (
    PENDING_PUBLISH_MARKER,
    ExtractionError,
    extract_archive,
    recover_pending_publish,
)


def _zip(tmp_path: Path, members: dict[str, bytes]) -> Path:
    import zipfile

    archive = tmp_path / "mod.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return archive


def test_successful_publish_leaves_no_marker(tmp_path):
    archive = _zip(tmp_path, {"a/one.esp": b"1", "b/two.esp": b"2"})
    target = tmp_path / "out"
    target.mkdir()
    extract_archive(archive, target)
    assert not (target / PENDING_PUBLISH_MARKER).exists()
    assert (target / "a" / "one.esp").read_bytes() == b"1"


def test_mid_publish_failure_leaves_a_marker_and_staging(tmp_path, monkeypatch):
    """The gap. A failure used to leave nothing to tell the next run.

    Now the marker and the staging dir both survive, so the publish can
    be finished rather than guessed at.
    """
    archive = _zip(tmp_path, {"a/one.esp": b"1", "b/two.esp": b"2"})
    target = tmp_path / "out"
    target.mkdir()

    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "move", flaky_move)
    with pytest.raises(ExtractionError):
        extract_archive(archive, target)

    assert (target / PENDING_PUBLISH_MARKER).exists(), "no marker left behind"
    # Staging is kept so the publish can be finished.
    assert any(p.name.startswith(".nexus-extract-") for p in target.iterdir())


def test_next_extract_repairs_a_pending_publish(tmp_path, monkeypatch):
    """The point of keeping staging: the next run finishes the job.

    Not "warn and carry on": the files that were in flight end up in the
    target, and the marker goes away.
    """
    archive = _zip(tmp_path, {"a/one.esp": b"1", "b/two.esp": b"2"})
    target = tmp_path / "out"
    target.mkdir()

    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "move", flaky_move)
    with pytest.raises(ExtractionError):
        extract_archive(archive, target)

    monkeypatch.setattr(shutil, "move", real_move)
    repaired = recover_pending_publish(target)
    assert repaired, "nothing was repaired"
    assert (target / "b" / "two.esp").read_bytes() == b"2"
    assert not (target / PENDING_PUBLISH_MARKER).exists()


def test_recover_with_nothing_pending_is_a_no_op(tmp_path):
    target = tmp_path / "out"
    target.mkdir()
    assert recover_pending_publish(target) == []


def test_marker_names_what_was_in_flight(tmp_path, monkeypatch):
    """A repair has to know what it is repairing."""
    archive = _zip(tmp_path, {"a/one.esp": b"1", "b/two.esp": b"2"})
    target = tmp_path / "out"
    target.mkdir()

    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "move", flaky_move)
    with pytest.raises(ExtractionError):
        extract_archive(archive, target)

    marker = (target / PENDING_PUBLISH_MARKER).read_text()
    assert "a/one.esp" in marker or "b/two.esp" in marker
