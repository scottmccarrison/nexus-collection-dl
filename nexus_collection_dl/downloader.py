"""Download manager with progress tracking."""

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


class DownloadError(Exception):
    """Raised when a download fails."""

    pass


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
        Download a mod file to a temporary location.

        Args:
            on_progress: Optional callback(bytes_downloaded, total_bytes) for
                         generic progress reporting (used by web UI / service layer).

        Returns path to the downloaded file.
        """
        mod_id = mod_info["mod_id"]
        file_id = mod_info["file_id"]
        filename = mod_info["filename"]

        # Get download URL from API
        try:
            download_url = self.api.get_download_url(game_domain, mod_id, file_id)
        except NexusAPIError as e:
            raise DownloadError(f"Failed to get download URL for {filename}: {e}")

        # Create temp directory for download
        target_dir.mkdir(parents=True, exist_ok=True)
        temp_path = target_dir / f".downloading_{filename}"

        try:
            response = self.session.get(download_url, stream=True)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))

            if progress and task_id is not None:
                progress.update(task_id, total=total_size)

            bytes_downloaded = 0
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        if progress and task_id is not None:
                            progress.update(task_id, advance=len(chunk))
                        if on_progress:
                            on_progress(bytes_downloaded, total_size)

            # Rename to final filename
            final_path = target_dir / filename
            temp_path.rename(final_path)
            return final_path

        except Exception as e:
            # Clean up temp file on error
            if temp_path.exists():
                temp_path.unlink()
            raise DownloadError(f"Failed to download {filename}: {e}")

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

        with Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=30),
            "[progress.percentage]{task.percentage:>3.0f}%",
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            for mod in mods:
                filename = mod["filename"]
                # Ensure size is an int (API may return string)
                size_bytes = mod.get("size_bytes") or 0
                if isinstance(size_bytes, str):
                    size_bytes = int(size_bytes) if size_bytes.isdigit() else 0
                task_id = progress.add_task(
                    "download",
                    filename=filename[:40],  # Truncate long names
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
                    results.append((mod, downloaded_path))

                    if on_complete:
                        on_complete(mod, downloaded_path)

                except DownloadError as e:
                    progress.console.print(f"[red]Error:[/red] {e}")
                    # Continue with other downloads

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
