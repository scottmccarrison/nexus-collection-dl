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
