"""Web UI for nexus-collection-dl."""

import argparse
import os
from pathlib import Path


def create_and_run(api_key: str | None = None, mods_dir: Path | None = None, port: int = 5000):
    """Create and run the Flask app."""
    from .app import create_app

    app = create_app(api_key=api_key, mods_dir=mods_dir)
    app.run(host="127.0.0.1", port=port, debug=False)


def main():
    """Standalone entry point for nexus-dl-web."""
    parser = argparse.ArgumentParser(description="nexus-dl web UI")
    parser.add_argument("mods_dir", type=Path, help="Mods directory")
    parser.add_argument("--port", type=int, default=5000, help="Port (default 5000)")
    parser.add_argument("--api-key", default=os.environ.get("NEXUS_API_KEY"), help="Nexus API key")
    args = parser.parse_args()

    create_and_run(api_key=args.api_key, mods_dir=args.mods_dir, port=args.port)
