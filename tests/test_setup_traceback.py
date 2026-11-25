"""#187: setup --non-interactive must refuse a bad root cleanly."""
from __future__ import annotations

from click.testing import CliRunner

from nexus_collection_dl import cli as cli_module


def test_setup_refuses_a_bad_collections_root(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    r = CliRunner().invoke(
        cli_module.main,
        ["setup", "--non-interactive", "--collections-root", "/nonexistent-root-xyz"],
    )
    assert r.exit_code != 0, r.output
    assert not isinstance(r.exception, PermissionError), (
        f"raw PermissionError leaked: {r.exception}"
    )
    assert "nonexistent-root-xyz" in (r.output + str(r.exception or "")), (
        r.output + str(r.exception or "")
    )
