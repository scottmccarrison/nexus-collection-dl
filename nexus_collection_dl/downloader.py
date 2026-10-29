"""Download manager with progress tracking and integrity verification."""

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable

import requests
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .api import NexusAPI, NexusAPIError

# Seconds: (connect timeout, per-read timeout) for download requests.
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 120

_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_URL_RE = re.compile(r"https?://\S+")


class DownloadError(Exception):
    """Raised when a download fails."""

    pass


def validate_download_filename(filename: Any) -> str:
    """Validate an API-provided filename as a single safe path component.

    Fails closed: an unsafe name is rejected with an error, never silently
    rewritten to a basename. Rejects empty names, path separators, absolute
    paths, drive letters, "." / "..", dotfile names, NUL bytes, and control
    characters.
    """
    if not isinstance(filename, str) or not filename:
        raise DownloadError(f"invalid download filename from API: {filename!r}")
    if "\x00" in filename:
        raise DownloadError(f"download filename contains a NUL byte: {filename!r}")
    if any(ord(c) < 32 for c in filename):
        raise DownloadError(f"download filename contains control characters: {filename!r}")
    if filename in (".", ".."):
        raise DownloadError(f"download filename is a dot name: {filename!r}")
    if filename.startswith("."):
        raise DownloadError(f"download filename is a hidden dotfile name: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise DownloadError(f"download filename contains a path separator: {filename!r}")
    if os.path.isabs(filename) or re.match(r"^[A-Za-z]:", filename):
        raise DownloadError(f"download filename is an absolute path: {filename!r}")
    if os.path.basename(filename) != filename:
        raise DownloadError(f"download filename is not a single component: {filename!r}")
    return filename


def _sanitize_message(text: str) -> str:
    """Strip URLs from an error message so signed links never surface."""
    return _URL_RE.sub("[url redacted]", text)


def _safe_error(exc: Exception) -> str:
    """Describe a download failure without exposing the signed download URL.

    requests exceptions embed the request URL in their text; download URLs
    carry premium credentials, so only error categories and status codes are
    reported, and any residual URL-looking text is redacted.
    """
    if isinstance(exc, requests.Timeout):
        return "request timed out"
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return f"HTTP error {status}" if status else "HTTP error"
    if isinstance(exc, requests.ConnectionError):
        return "connection error"
    if isinstance(exc, requests.RequestException):
        return f"network error ({type(exc).__name__})"
    return _sanitize_message(f"{type(exc).__name__}: {exc}")


def _expected_size(mod_info: dict[str, Any]) -> int | None:
    """Exact expected byte size from trusted metadata, or None when unknown.

    Only true byte counts are enforced. The legacy REST API reports sizes in
    KB, which must never be treated as an exact byte count, so unknown or
    non-positive sizes mean "skip the size check".
    """
    raw = mod_info.get("size_bytes")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        return value if value > 0 else None
    return None


def _expected_md5(mod_info: dict[str, Any]) -> str | None:
    """Expected MD5 from already-available metadata, or None.

    Only well-formed 32-hex-digit hashes are enforced; anything else is
    skipped rather than guessed at. No extra API calls are made to obtain it.
    """
    raw = mod_info.get("expected_md5") or mod_info.get("md5")
    if isinstance(raw, str) and _MD5_RE.match(raw.strip()):
        return raw.strip().lower()
    return None


class Downloader:
    """Handles mod file downloads with progress tracking."""

    def __init__(self, api: NexusAPI):
        self.api = api
        self.session = requests.Session()

    def download_mod(
        self,
        game_domain: str,
        mod_info: dict[str, Any],
        target_dir: Path,
        progress: Progress | None = None,
        task_id: TaskID | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """
        Download a mod file to a temporary location, verify it, and publish
        it atomically.

        Safety guarantees:
          - the filename is validated as a single safe component (traversal,
            absolute paths, separators, and dot names are rejected),
          - the download streams to a uniquely named temp file in the target
            directory with request timeouts, and the temp file and response
            are always cleaned up,
          - when trusted metadata provides an exact byte size (> 0) or a
            well-formed MD5, the download is verified BEFORE the final file
            is published; unknown sizes/hashes skip that check,
          - the final file is published with an atomic same-directory rename,
            so an existing final file is never clobbered by a failed or
            corrupt download,
          - error messages never include the signed download URL.

        Args:
            on_progress: Optional callback(bytes_downloaded, total_bytes) for
                         generic progress reporting (used by web UI / service layer).

        Returns path to the downloaded file.
        """
        mod_id = mod_info["mod_id"]
        file_id = mod_info["file_id"]
        filename = validate_download_filename(mod_info.get("filename"))
        expected_size = _expected_size(mod_info)
        expected_md5 = _expected_md5(mod_info)

        # Get download URL from API
        try:
            download_url = self.api.get_download_url(game_domain, mod_id, file_id)
        except NexusAPIError as e:
            raise DownloadError(
                f"Failed to get download URL for {filename}: {_sanitize_message(str(e))}"
            )
        if not download_url:
            raise DownloadError(f"API returned no download URL for {filename}")

        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / filename

        # Unique temp file in the target directory: same filesystem as the
        # final path (so the publish rename is atomic) and never colliding
        # with concurrent downloads or a real mod filename.
        fd, tmp_name = tempfile.mkstemp(prefix=".nexus-download-", dir=target_dir)
        os.close(fd)
        temp_path = Path(tmp_name)

        try:
            response = self.session.get(
                download_url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            )
            try:
                response.raise_for_status()

                header_length = response.headers.get("content-length")
                try:
                    total_size = int(header_length) if header_length else 0
                except ValueError:
                    total_size = 0

                if progress and task_id is not None:
                    progress.update(task_id, total=total_size)

                hasher = hashlib.md5()
                bytes_downloaded = 0
                with open(temp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)
                            bytes_downloaded += len(chunk)
                            if progress and task_id is not None:
                                progress.update(task_id, advance=len(chunk))
                            if on_progress:
                                on_progress(bytes_downloaded, total_size)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                response.close()

            # Verify against trusted metadata before publishing. Failures
            # leave any existing final file untouched.
            if expected_size is not None and bytes_downloaded != expected_size:
                raise DownloadError(
                    f"{filename}: size mismatch (expected {expected_size} "
                    f"bytes, got {bytes_downloaded}); the download is "
                    f"truncated or the wrong file"
                )
            if expected_md5 is not None:
                actual_md5 = hasher.hexdigest().lower()
                if actual_md5 != expected_md5:
                    raise DownloadError(
                        f"{filename}: MD5 mismatch (expected {expected_md5}, "
                        f"got {actual_md5}); the download is corrupt or the "
                        f"wrong file"
                    )

            # Atomic publish: replaces the final file only now that the
            # payload is fully downloaded and verified.
            os.replace(temp_path, final_path)
            return final_path

        except DownloadError:
            raise
        except Exception as e:
            raise DownloadError(f"Failed to download {filename}: {_safe_error(e)}")
        finally:
            # Always drop the temp file: after a successful publish it no
            # longer exists, and on any failure (including Ctrl+C) no partial
            # download is left behind. The final file is never touched here.
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def download_mods(
        self,
        game_domain: str,
        mods: list[dict[str, Any]],
        target_dir: Path,
        on_complete: Callable[[dict[str, Any], Path], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[tuple[dict[str, Any], Path]]:
        """
        Download multiple mods with a unified progress display.

        Args:
            game_domain: Game domain (e.g., 'baldursgate3')
            mods: List of mod info dicts
            target_dir: Directory to download files to
            on_complete: Callback called after each successful download
            on_progress: Optional callback(bytes_downloaded, total_bytes) for
                         generic progress (passed through to download_mod).
                         When provided, the Rich progress bar is still shown for
                         CLI usage, and the callback fires alongside it.

        Returns list of (mod_info, downloaded_path) tuples.
        """
        results = []
        total_mods = len(mods)

        with Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=30),
            "[progress.percentage]{task.percentage:>3.0f}%",
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            for i, mod in enumerate(mods, 1):
                try:
                    # Validate before slicing or logging the raw API-provided
                    # name, so malformed input is never echoed or used.
                    filename = validate_download_filename(mod.get("filename"))
                except DownloadError as e:
                    progress.console.print(f"[red]Error:[/red] {e}")
                    continue
                # Ensure size is an int (API may return string)
                size_bytes = mod.get("size_bytes") or 0
                if isinstance(size_bytes, str):
                    size_bytes = int(size_bytes) if size_bytes.isdigit() else 0

                progress.console.print(
                    f"[dim]({i}/{total_mods})[/dim] {filename}"
                )
                task_id = progress.add_task(
                    "download",
                    filename=filename[:40],
                    total=size_bytes,
                )

                try:
                    downloaded_path = self.download_mod(
                        game_domain=game_domain,
                        mod_info=mod,
                        target_dir=target_dir,
                        progress=progress,
                        task_id=task_id,
                        on_progress=on_progress,
                    )
                    progress.remove_task(task_id)
                    results.append((mod, downloaded_path))

                    if on_complete:
                        on_complete(mod, downloaded_path)

                except DownloadError as e:
                    progress.remove_task(task_id)
                    progress.console.print(f"[red]Error:[/red] {e}")

        return results


def create_download_progress() -> Progress:
    """Create a progress bar for downloads."""
    return Progress(
        TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
        BarColumn(bar_width=30),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
