"""Check for newer releases on GitHub."""

import urllib.request
import json

from . import __version__

GITHUB_REPO = "scottmccarrison/nexus-collection-dl"
TIMEOUT_SECONDS = 2


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '0.2.0' into a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


def check_for_update() -> str | None:
    """Return a message if a newer version is available, or None.

    Returns None silently on any failure (offline, timeout, no releases).
    """
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "")
        latest = tag.lstrip("v")
        if _parse_version(latest) > _parse_version(__version__):
            return (
                f"nexus-dl v{__version__} - update available (v{latest}). "
                f"Run: pip install --upgrade nexus-collection-dl"
            )
    except Exception:
        return None
    return None
