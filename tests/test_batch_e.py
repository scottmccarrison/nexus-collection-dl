"""Batch E fixes: #183 fallback, #155 misleading output, #159 escape hatch."""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# #183: a bad --game-dir or --prefix is an error, not a fallback
# ---------------------------------------------------------------------------


def test_deploy_rejects_a_nonexistent_game_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n",
        "collection_revision": 1, "game_domain": "skyrimspecialedition",
        "mods": {"1": {"mod_id": 1, "name": "M", "file_id": 1, "filename": "m.zip",
                       "version": "1", "phase": 0, "enabled": True, "optional": False,
                       "contributed_files": [], "requirements": []}},
        "tombstones": {},
    }))
    (mods / "Data").mkdir()
    (mods / "Data" / "m.esp").write_text("x")
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module
    r = CliRunner().invoke(
        cli_module.main,
        ["deploy", str(mods), "--game-dir", "/nonexistent-path-xyz"],
    )
    assert r.exit_code != 0, r.output
    assert "not an existing directory" in r.output.lower() or "nonexistent" in r.output.lower(), r.output


def test_deploy_rejects_a_nonexistent_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n",
        "collection_revision": 1, "game_domain": "skyrimspecialedition",
        "mods": {"1": {"mod_id": 1, "name": "M", "file_id": 1, "filename": "m.zip",
                       "version": "1", "phase": 0, "enabled": True, "optional": False,
                       "contributed_files": [], "requirements": []}},
        "tombstones": {},
    }))
    (mods / "Data").mkdir()
    (mods / "Data" / "m.esp").write_text("x")
    game = tmp_path / "game"
    game.mkdir()
    from click.testing import CliRunner

    from nexus_collection_dl import cli as cli_module
    r = CliRunner().invoke(
        cli_module.main,
        ["deploy", str(mods), "--game-dir", str(game), "--prefix", "/nonexistent-prefix-xyz"],
    )
    assert r.exit_code != 0, r.output
    assert "not an existing directory" in r.output.lower() or "nonexistent" in r.output.lower(), r.output


# ---------------------------------------------------------------------------
# #155: --no-extract must not say "Extracting..."
# ---------------------------------------------------------------------------


def test_no_extract_does_not_say_extracting(tmp_path, monkeypatch):
    """The progress label must match what is actually happening."""
    from unittest.mock import MagicMock, patch

    from nexus_collection_dl import service as service_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    state_dir = tmp_path / "mods"
    state_dir.mkdir()
    from nexus_collection_dl.state import CollectionState
    s = CollectionState(state_dir)
    s.set_collection_info("u", "n", 1, "skyrimspecialedition")
    s.save()

    svc = service_module.ModManagerService(api_key="k")
    svc._api = MagicMock()
    svc._api.get_mod_info.return_value = {"name": "X", "version": "1"}
    svc._api.get_mod_files.return_value = [
        {"file_id": 1, "name": "x.zip", "file_name": "x.zip", "version": "1"}
    ]
    svc._api.validate_key.return_value = {"is_premium": True}
    svc._api.get_mod_requirements.return_value = []

    progress_messages = []
    def track_progress(event, pct, msg):
        progress_messages.append((event, msg))

    with patch.object(service_module, "Downloader") as mock_dl, \
         patch.object(service_module, "extract_archive", return_value=[]) as mock_extract:
        mock_dl.return_value.download_mods.return_value = [
            ("mod", state_dir / "x.zip")
        ]
        result = svc.add_mod(
            "https://www.nexusmods.com/skyrimspecialedition/mods/9",
            state_dir,
            no_extract=True,
            on_progress=track_progress,
        )
    assert result.success, result.error
    assert not mock_extract.called, "extract_archive must not be called with --no-extract"
    extracting_msgs = [m for _, m in progress_messages if "extract" in m.lower()]
    assert not extracting_msgs, f"no-extract must not report extraction: {extracting_msgs}"


# ---------------------------------------------------------------------------
# #159: the redaction diagnostic escape must be reachable
# ---------------------------------------------------------------------------


def test_boundary_redact_diagnostic_flag_exists():
    """The diagnostic escape must be reachable from a user, not just a kwarg."""
    import inspect

    from nexus_collection_dl.redaction import boundary_redact
    sig = inspect.signature(boundary_redact)
    assert "diagnostic" in sig.parameters, "boundary_redact must accept diagnostic"


def test_nexus_dl_diagnostic_env_var_enables_escape(monkeypatch):
    """A maintainer sets NEXUS_DL_DIAGNOSTIC=1 to see unredacted text."""
    from nexus_collection_dl.redaction import boundary_redact
    monkeypatch.setenv("NEXUS_DL_DIAGNOSTIC", "1")
    out = boundary_redact("error with secret abc123", ("abc123",))
    assert "diagnostic" in out.lower(), f"env var must enable diagnostic mode: {out}"
    assert "abc123" in out, "diagnostic mode must show the secret"


def test_nexus_dl_diagnostic_env_var_off_by_default(monkeypatch):
    """Without the env var, redaction is one-way."""
    from nexus_collection_dl.redaction import boundary_redact
    monkeypatch.delenv("NEXUS_DL_DIAGNOSTIC", raising=False)
    out = boundary_redact("error with secret abc123", ("abc123",))
    assert "abc123" not in out, "redaction must hide the secret by default"
