"""Reclaim disk from archives whose job is done.

Downloaded archives pile up: `--no-extract` retains them, re-runs add
more, and multiple collections add more again. Manual deletion is easy
to get wrong. This is the careful version.

Dry-run by default. It reports what is reclaimable and touches nothing
until `--apply`. Two hard rules:

- The backup store (`.nexus-backups/`) is what undeploy restores from.
  Never deleted.
- An archive that is the only copy of its mod is never deleted. The test
  for "only copy" is attribution: a mod that has extracted payload on
  disk has its content, and the archive is redundant. A mod with no
  attribution (extracted before that existed) or with nothing on disk
  still needs its archive, and it stays.

An archive the state does not claim is not ours to delete and is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .deploy import BACKUPS_DIRNAME
from .state import CollectionState, StateError

# Archive extensions the download path produces. Deliberately narrow: a
# file nobody claims is kept whatever it is, and this only narrows which
# files are candidates in the first place.
ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".rar"})


@dataclass
class GcPlan:
    """What a gc run would reclaim, and what it would keep and why."""

    reclaimable: list[Path] = field(default_factory=list)
    bytes_reclaimable: int = 0
    kept: list[tuple[Path, str]] = field(default_factory=list)


def plan_gc(mods_dir: Path) -> GcPlan:
    """Dry-run: what is reclaimable and what is kept. Touches nothing."""
    plan = GcPlan()
    mods_dir = Path(mods_dir)

    try:
        state = CollectionState(mods_dir)
        state.load()
    except StateError:
        # No state means no claims on any archive: nothing is ours.
        return plan

    # filename -> ModState, so an archive is reclaimable only when the
    # mod it belongs to is accounted for.
    by_filename = {ms.filename: ms for ms in state.mods.values()}

    for path in sorted(mods_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(mods_dir)
        if rel.parts and rel.parts[0] == BACKUPS_DIRNAME:
            # The backup store. Never ours to delete.
            continue
        if path.suffix.lower() not in ARCHIVE_SUFFIXES:
            continue

        mod = by_filename.get(path.name)
        if mod is None:
            plan.kept.append((path, "not claimed by any mod in state"))
            continue
        if not mod.contributed_files:
            plan.kept.append(
                (path, "no extracted payload recorded for this mod")
            )
            continue
        # The mod has attribution. If any of its files are still on the
        # disk the archive is redundant; if none are, the archive is the
        # only copy.
        if not any((mods_dir / f).is_file() for f in mod.contributed_files):
            plan.kept.append((path, "the archive is the only copy of this mod"))
            continue

        plan.reclaimable.append(path)
        plan.bytes_reclaimable += path.stat().st_size

    return plan


def apply_gc(plan: GcPlan) -> int:
    """Delete what the plan says is reclaimable. Returns bytes freed."""
    freed = 0
    for path in plan.reclaimable:
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
        except OSError:
            continue
    return freed
