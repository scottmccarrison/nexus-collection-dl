"""What the game looks like from here, for FOMOD installer conditions.

``fileDependency`` and ``gameDependency`` conditions in a FOMOD installer
ask about the install target: is ``Master.esm`` active, is the game new
enough. Without answers the plan is indeterminate and the mod falls back
to the folder heuristic, which is how a multi-game installer like NVAC
ends up installing the wrong branch.

The answers come from the game's own load order: ``plugins.txt`` in the
Proton prefix. That is what the game will see, so it is what the
installer should see.

Unknown is not empty. A missing ``plugins.txt`` means we do not know what
is active, not that nothing is, and the caller's tri-state evaluation
turns that into "indeterminate" rather than a guess.
"""

from __future__ import annotations

from pathlib import Path

from .deploy import get_plugins_txt_dest
from .fomod_plan import GameState

# plugins.txt uses a leading `*` for enabled. Anything without one is
# present but off. Anything not listed at all is Missing.
ACTIVE_PREFIX = "*"


def read_plugin_states(plugins_txt: Path) -> dict[str, str]:
    """Plugin name (lowercased) to Active/Inactive from a plugins.txt."""
    states: dict[str, str] = {}
    try:
        text = plugins_txt.read_text()
    except OSError:
        return states
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        active = stripped.startswith(ACTIVE_PREFIX)
        name = stripped.lstrip(ACTIVE_PREFIX).strip()
        if not name:
            continue
        states[name.lower()] = "Active" if active else "Inactive"
    return states


def read_game_state(prefix: Path | None, game_domain: str) -> GameState:
    """Build the GameState a FOMOD installer's conditions are answered against.

    Reads the game's own plugins.txt from the Proton prefix. When there
    is no prefix, no plugins.txt, or no mapping for this game, the
    plugin states are unknown (``None``) rather than empty: refusing to
    guess is what keeps a wrong branch from being picked.

    ``game_version`` stays ``None`` for now. Reading a game's version out
    of its own binaries is its own job; a ``gameDependency`` condition is
    still undecidable until that exists.
    """
    if prefix is None:
        return GameState()
    dest = get_plugins_txt_dest(prefix, game_domain)
    if dest is None or not dest.is_file():
        return GameState()
    states = read_plugin_states(dest)
    return GameState(plugin_states=states)
