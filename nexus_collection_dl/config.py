"""User-level configuration for nexus-dl.

The keystone of the setup and front-door milestone. Before this, every
command re-asked for its arguments and nothing noticed a fresh install.
With it: first-run detection (this file is missing or unusable), a
wizard that writes it, bare ``nexus-dl`` booting the shell, and defaults
for the commands that used to demand a path.

Lives in the XDG config directory next to ``api-key`` and
``handler.json`` (see ``handler.config_dir``).

Two contracts it shares with ``CollectionState``:

- The save is atomic: unique temp file, fsync, rename. A crash mid-write
  cannot leave a half-written config behind.
- Unknown top-level keys survive a save. Known keys come from the
  in-memory object; anything else is carried forward from the file. That
  is forward compatibility, not retroactive repair: it keeps the next new
  key from vanishing when someone runs a slightly older build against the
  same file.

``is_configured`` is deliberately "has a collections root", not "has a
file". A config that exists but names nowhere to put collections is
still a first run.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errmsg import exception_reason
from .handler import config_dir

CONFIG_FILENAME = "config.json"


class ConfigError(Exception):
    """Raised when the user-level config cannot be read or written."""


@dataclass
class UserConfig:
    """User-level settings. See the module docstring for the contracts."""

    # Where collection directories live. The parent root a `sync` target
    # sits under, not the collection directory itself.
    collections_root: str = ""
    # Known games: [{"domain", "game_dir", "prefix"}]. The wizard fills
    # this; `deploy` and `handler map` can consult it instead of asking.
    games: list[dict] = field(default_factory=list)
    # Free-form defaults for later milestones (deploy method, notify
    # policy). Reserved so the shape is stable.
    defaults: dict = field(default_factory=dict)

    @classmethod
    def path(cls) -> Path:
        """Location of the config file (XDG-aware, next to the API key)."""
        return config_dir() / CONFIG_FILENAME

    def is_configured(self) -> bool:
        """True when the tool has somewhere to put collections.

        Not "the file exists": a config with no collections root is
        still a first run, because there is nowhere to put anything.
        """
        return bool(self.collections_root)

    @classmethod
    def load(cls) -> UserConfig:
        """Read the config. A missing or unreadable one is a first run.

        Never raises for a missing or corrupt file: those are the states
        the front door acts on, not errors. A file that exists but cannot
        be parsed yields an empty config so the caller sees a first run.
        """
        path = cls.path()
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        games = data.get("games") or []
        defaults = data.get("defaults") or {}
        return cls(
            collections_root=str(data.get("collections_root") or ""),
            games=list(games) if isinstance(games, list) else [],
            defaults=dict(defaults) if isinstance(defaults, dict) else {},
        )

    def save(self) -> None:
        """Write the config atomically, preserving unknown keys."""
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "collections_root": self.collections_root,
            "games": self.games,
            "defaults": self.defaults,
        }

        # Forward compatibility: carry over anything this version does
        # not model. Known keys always come from the in-memory object.
        try:
            with open(path) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = None
        if isinstance(existing, dict):
            for key, value in existing.items():
                if key not in data:
                    data[key] = value

        # Temp file in the same directory so the rename stays on one
        # filesystem.
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f"{CONFIG_FILENAME}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception as e:
            # A serialization failure must not leave the temp file or a
            # half-written config behind.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise ConfigError(f"could not save the user config: {exception_reason(e)}") from e
