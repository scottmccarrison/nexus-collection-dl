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
  still needs its archive, and it stays. The archive itself never counts
  as its own payload.

An archive is recognised by content, not by a suffix list: sync names
downloads after the manifest's filename, which for community collections
is often a display name with no extension. An archive the state does not
claim is not ours to delete and is kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .deploy import BACKUPS_DIRNAME
from .extractor import is_archive
from .state import CollectionState, ModState, StateError, _is_safe_relative


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

    # A mod's archive lives at one fixed place: mods_dir / filename.
    # Matching on basename alone let a same-named file in any subfolder
    # be reclaimed as if it were that archive.
    claimed: dict[Path, ModState] = {}
    for ms in state.mods.values():
        if not ms.filename or not _is_safe_relative(ms.filename):
            continue
        claimed[mods_dir / ms.filename] = ms

    for path in sorted(mods_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(mods_dir)
        if rel.parts and rel.parts[0] == BACKUPS_DIRNAME:
            # The backup store. Never ours to delete.
            continue
        if not is_archive(path):
            # Content sniffing, not a suffix list: sync names archives
            # after the manifest's filename, which for community
            # collections is a display name with no extension. A loose
            # file download is its own payload and is never a candidate.
            continue

        mod = claimed.get(path)
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
        # complete copy. ALL payload files must be on disk: if extraction
        # was interrupted, the archive is the only complete copy. The
        # archive itself does not count as payload: a
        # retained archive whose attribution is just its own name is
        # still the only copy.
        payload_names = [
            f for f in mod.contributed_files
            if f != mod.filename and _is_safe_relative(f)
        ]
        if not payload_names or not all((mods_dir / f).is_file() for f in payload_names):
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


def human_bytes(n: int) -> str:
    """Sizes the way a person reads them: 512 B, 4.2 KB, 10.2 GB."""
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


# Below this much reclaimable archive disk, saying something is noise.
NUDGE_THRESHOLD_BYTES = 100 * 1024 * 1024


def nudge_message(mods_dir: Path, threshold_bytes: int = NUDGE_THRESHOLD_BYTES) -> str | None:
    """A one-line visibility notice, or None when there is nothing to say.

    The user owns whether to run gc; this is the tool making sure the
    reclaimable disk is visible while they are working. Silent below the
    threshold and when there is nothing to reclaim.
    """
    plan = plan_gc(mods_dir)
    if plan.bytes_reclaimable < threshold_bytes:
        return None
    return (
        f"{human_bytes(plan.bytes_reclaimable)} of downloaded archives "
        f"can be reclaimed. Run 'nexus-dl gc {mods_dir} --apply'."
    )
