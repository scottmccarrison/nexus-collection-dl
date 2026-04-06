"""Auto-update checker for nexus-collection-dl (git clone installs)."""

import json
import subprocess
import time
from pathlib import Path

import click


CACHE_DIR = Path.home() / ".cache" / "nexus-dl"
CACHE_FILE = CACHE_DIR / "version-check.json"
CACHE_TTL = 3600  # 1 hour
GITHUB_REPO = "scottmccarrison/nexus-collection-dl"


def get_current_version() -> str:
    """Get the currently installed version via importlib.metadata."""
    import importlib.metadata

    return importlib.metadata.version("nexus-collection-dl")


def _read_cache() -> dict | None:
    """Read cached version check result if still fresh."""
    try:
        data = json.loads(CACHE_FILE.read_text())
        if time.time() - data["last_check"] < CACHE_TTL:
            return data
    except (FileNotFoundError, KeyError, json.JSONDecodeError, OSError):
        pass
    return None


def _write_cache(latest: str) -> None:
    """Write version check result to cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps({
            "last_check": time.time(),
            "latest": latest,
        }))
    except OSError:
        pass


def get_latest_release() -> tuple[str, str] | None:
    """Fetch latest release from GitHub API.

    Returns (version, html_url) or None on any error.
    """
    import requests

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=3,
        )
        resp.raise_for_status()
        data = resp.json()
        tag = data["tag_name"].lstrip("v")
        return (tag, data["html_url"])
    except Exception:
        return None


def check_for_update() -> tuple[str, str, str] | None:
    """Check if a newer version is available.

    Returns (current, latest, release_url) if update available, else None.
    Uses a 1-hour cache to avoid hammering GitHub.
    """
    from packaging.version import parse

    current = get_current_version()

    # Check cache first
    cached = _read_cache()
    if cached:
        latest = cached["latest"]
        if parse(latest) > parse(current):
            url = f"https://github.com/{GITHUB_REPO}/releases/tag/v{latest}"
            return (current, latest, url)
        return None

    # Fetch from GitHub
    result = get_latest_release()
    if result is None:
        return None

    latest, release_url = result
    _write_cache(latest)

    if parse(latest) > parse(current):
        return (current, latest, release_url)
    return None


def do_update() -> bool:
    """Pull latest code and reinstall via pip.

    Returns True on success, False on failure.
    """
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution("nexus-collection-dl")
        src_dir = Path(dist.locate_file(""))
    except Exception:
        return False

    try:
        subprocess.run(
            ["git", "-C", str(src_dir), "pull", "--ff-only"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["pip", "install", "-e", str(src_dir)],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def check_and_prompt_update(console) -> None:
    """Check for updates and prompt the user to install.

    Called from CLI on every invocation (unless --skip-update-check).
    Silently returns on any error - never blocks the user.
    """
    try:
        result = check_for_update()
    except Exception:
        return

    if result is None:
        return

    current, latest, release_url = result
    console.print(f"[yellow]Update available:[/yellow] v{current} -> v{latest}")
    console.print(f"[dim]{release_url}[/dim]")

    if click.confirm("Update now?", default=True):
        console.print("[dim]Updating...[/dim]")
        if do_update():
            console.print("[green]Updated successfully![/green] Restart to use the new version.")
        else:
            console.print("[red]Update failed.[/red] Try manually: git pull && pip install -e .")
