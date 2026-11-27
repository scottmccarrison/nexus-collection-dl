"""Profile switching: named sets of collections and mods for one game.

A profile is a playlist. Create it empty, add collections and mods to
it, then deploy it as a unit. Switching profiles undeploys the current
set and deploys the new one.

Profiles live in the user config, not in collection state: a profile
spans collections. Each profile has a stable numeric ID assigned at
creation. Commands accept either the ID (bare number) or the name
(string).

`detect_ref` figures out what kind of thing the user passed: a
collection URL, a mod URL, a bare mod ID, or a collection name. No
flags needed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import UserConfig

_COLLECTION_URL_RE = re.compile(r"/collections/([^/?#]+)")
_MOD_URL_RE = re.compile(r"/mods/(\d+)")
_NXM_RE = re.compile(r"^nxm://[^/]+/mods/(\d+)")


class ProfileError(Exception):
    """Raised for profile operations that cannot proceed."""


def detect_ref(value: str | int) -> tuple[str, Any]:
    """Figure out what kind of mod-or-collection reference this is.

    Returns ("collection", <name-or-slug>) or ("mod", <mod_id>).
    """
    if isinstance(value, int):
        return ("mod", value)
    s = str(value).strip()
    if not s:
        raise ProfileError("Empty reference.")

    # nxm:// links
    m = _NXM_RE.match(s)
    if m:
        return ("mod", int(m.group(1)))

    # URL with /collections/slug
    if "://" in s:
        m = _COLLECTION_URL_RE.search(s)
        if m:
            return ("collection", m.group(1))
        m = _MOD_URL_RE.search(s)
        if m:
            return ("mod", int(m.group(1)))
        raise ProfileError(f"Cannot tell what {s!r} is. Expected a Nexus URL.")

    # Bare number -> mod ID
    if s.isdigit():
        return ("mod", int(s))

    # Anything else -> collection name
    return ("collection", s)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _load_profiles() -> tuple[dict[str, Any], int]:
    """Load profiles dict and next_id counter from config."""
    cfg = UserConfig.load()
    raw = cfg.defaults.get("profiles") or {}
    profiles = dict(raw) if isinstance(raw, dict) else {}
    next_id = int(cfg.defaults.get("next_profile_id") or 1)
    return profiles, next_id


def _save_profiles(profiles: dict[str, Any], next_id: int) -> None:
    cfg = UserConfig.load()
    cfg.defaults["profiles"] = profiles
    cfg.defaults["next_profile_id"] = next_id
    cfg.save()


def list_profiles() -> list[dict[str, Any]]:
    """All profiles, sorted by ID."""
    profiles, _ = _load_profiles()
    out = []
    for pid in sorted(profiles.keys(), key=lambda k: int(k)):
        p = dict(profiles[pid])
        p["id"] = int(pid)
        out.append(p)
    return out


def get_profile(profile: int | str) -> dict[str, Any]:
    """One profile by ID or name."""
    pid = str(resolve_profile(profile))
    profiles, _ = _load_profiles()
    if pid not in profiles:
        raise ProfileError(f"No profile with id {pid}.")
    p = dict(profiles[pid])
    p["id"] = int(pid)
    return p


def resolve_profile(value: int | str) -> int:
    """Resolve a profile ID or name to its numeric ID."""
    profiles, _ = _load_profiles()
    # Try as ID
    if isinstance(value, int) or str(value).isdigit():
        pid = str(int(value))
        if pid in profiles:
            return int(pid)
    # Try as name
    for pid, p in profiles.items():
        if p.get("name") == value:
            return int(pid)
    raise ProfileError(f"No profile matched {value!r}.")


def create_profile(name: str, game: str = "", game_dir: str = "") -> int:
    """Create an empty profile. Returns its ID."""
    profiles, next_id = _load_profiles()
    # Check for name collision
    for p in profiles.values():
        if p.get("name") == name:
            raise ProfileError(f"A profile named {name!r} already exists.")
    pid = str(next_id)
    profiles[pid] = {
        "name": name,
        "game": game,
        "game_dir": game_dir,
        "collections": [],
        "mods": [],
    }
    _save_profiles(profiles, next_id + 1)
    return next_id


def add_to_profile(profile: int | str, kind: str, value: Any) -> None:
    """Add a collection or mod to a profile. Duplicates are ignored."""
    pid = str(resolve_profile(profile))
    profiles, next_id = _load_profiles()
    p = profiles[pid]
    if kind == "collection":
        if value not in p["collections"]:
            p["collections"].append(value)
    elif kind == "mod":
        vid = int(value)
        if vid not in p["mods"]:
            p["mods"].append(vid)
    else:
        raise ProfileError(f"Unknown kind {kind!r}. Expected 'collection' or 'mod'.")
    _save_profiles(profiles, next_id)


def remove_from_profile(profile: int | str, kind: str, value: Any) -> None:
    """Remove a collection or mod from a profile."""
    pid = str(resolve_profile(profile))
    profiles, next_id = _load_profiles()
    p = profiles[pid]
    if kind == "collection":
        p["collections"] = [c for c in p["collections"] if c != value]
    elif kind == "mod":
        vid = int(value)
        p["mods"] = [m for m in p["mods"] if m != vid]
    else:
        raise ProfileError(f"Unknown kind {kind!r}. Expected 'collection' or 'mod'.")
    _save_profiles(profiles, next_id)


def duplicate_profile(profile: int | str, new_name: str) -> int:
    """Copy a profile under a new name. Returns the new ID."""
    src = get_profile(profile)
    profiles, next_id = _load_profiles()
    for p in profiles.values():
        if p.get("name") == new_name:
            raise ProfileError(f"A profile named {new_name!r} already exists.")
    pid = str(next_id)
    profiles[pid] = {
        "name": new_name,
        "game": src.get("game", ""),
        "game_dir": src.get("game_dir", ""),
        "collections": list(src.get("collections", [])),
        "mods": list(src.get("mods", [])),
    }
    _save_profiles(profiles, next_id + 1)
    return next_id


def delete_profile(profile: int | str) -> None:
    """Delete a profile by ID or name."""
    pid = str(resolve_profile(profile))
    profiles, next_id = _load_profiles()
    profiles.pop(pid, None)
    _save_profiles(profiles, next_id)


# ---------------------------------------------------------------------------
# Deploy integration
# ---------------------------------------------------------------------------


def use_profile(
    profile: int | str,
    collections_root: Path | None = None,
    game_dir: Path | None = None,
) -> dict[str, Any]:
    """Deploy a profile: undeploy the current set, deploy the new one.

    Returns {"deployed": <count>, "undeployed": <count>, "errors": [...]}.
    """
    from pathlib import Path as P

    from .config import UserConfig
    from .service import ModManagerService, scan_collections

    prof = get_profile(profile)
    cfg = UserConfig.load()
    root = P(collections_root or cfg.collections_root)

    # Resolve game dir: explicit > profile > config
    resolved_game = game_dir
    if resolved_game is None:
        gd = prof.get("game_dir")
        if gd:
            resolved_game = P(gd)
    if resolved_game is None:
        for g in cfg.games:
            if g.get("domain") == prof.get("game"):
                gd = g.get("game_dir")
                if gd:
                    resolved_game = P(gd)
                    break
    if resolved_game is None:
        raise ProfileError(
            "No game directory. Pass one explicitly or set it on the profile."
        )

    svc = ModManagerService()
    errors: list[str] = []
    deployed = 0
    undeployed = 0

    # 1. Undeploy whatever is currently deployed across all collections
    scan = scan_collections(root)
    for s in scan.collections:
        if not hasattr(s, "dir"):
            continue
        coll_dir = root / s.name
        state_file = coll_dir / ".nexus-state.json"
        if not state_file.exists():
            continue
        try:
            state = _load_state(coll_dir)
            if state.deployed_files:
                r = svc.undeploy(coll_dir, game_dir=resolved_game)
                undeployed += getattr(r, "removed_count", 0) or len(state.deployed_files)
        except Exception as e:
            from .errmsg import exception_reason
            errors.append(f"undeploy {s.name}: {exception_reason(e)}")

    # 2. Deploy each collection in the profile
    for coll_name in prof.get("collections", []):
        coll_dir = root / coll_name
        if not (coll_dir / ".nexus-state.json").exists():
            errors.append(f"collection {coll_name!r} not found under {root}")
            continue
        try:
            result = svc.deploy(coll_dir, game_dir=resolved_game)
            deployed += int(getattr(result, "deployed_count", 0) or 0)
            for msg in (getattr(result, "errors", None) or []):
                errors.append(f"{coll_name}: {msg}")
        except Exception as e:
            from .errmsg import exception_reason
            errors.append(f"deploy {coll_name}: {exception_reason(e)}")

    # 3. Individual mods (from any collection)
    for mod_id in prof.get("mods", []):
        # Find which collection has this mod
        found = False
        for s in scan.collections:
            coll_dir = root / s.name
            if not (coll_dir / ".nexus-state.json").exists():
                continue
            try:
                state = _load_state(coll_dir)
                if any(m.mod_id == mod_id for m in state.mods.values()):
                    result = svc.deploy(coll_dir, game_dir=resolved_game)
                    deployed += int(getattr(result, "deployed_count", 0) or 0)
                    found = True
                    break
            except Exception:  # noqa: S112
                continue
        if not found:
            errors.append(f"mod {mod_id} not found in any collection")

    return {"deployed": deployed, "undeployed": undeployed, "errors": errors}


def _load_state(coll_dir):
    from .state import CollectionState
    state = CollectionState(coll_dir)
    state.load()
    return state
