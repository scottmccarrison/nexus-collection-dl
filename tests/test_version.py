"""Packaging metadata consistency."""

import re
from pathlib import Path

from nexus_collection_dl import __version__

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_pyproject_version_matches_package_version():
    """The distribution version and the reported version never drift."""
    text = PYPROJECT.read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "no version field found in pyproject.toml"
    assert match.group(1) == __version__
