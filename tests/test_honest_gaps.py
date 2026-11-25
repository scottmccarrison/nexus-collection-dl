"""Say what is actually true about two things the tool currently misreports.

- a handler mapping that has gone stale is dropped silently and the
  error talks as if nothing was ever mapped. The user made a mapping;
  the tool should say what became of it.
- `add`'s "missing dependency" warning cannot fire in practice: the REST
  mod endpoint it reads carries no requirements at all. The code stays
  silent rather than inventing an all-clear, which is right - but the
  docstring calls it "the `add` dependency warning" as if it were a
  thing a user will see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus_collection_dl.handler import (
    HandlerConfig,
    NxmRoutingError,
    resolve_collection_dir,
)
from nexus_collection_dl.service import (
    _requirements_from_mod_info,
    missing_requirements,
)
from nexus_collection_dl.state import CollectionState

DOMAIN = "skyrimspecialedition"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NEXUS_DL_STEAM_ROOT", str(tmp_path / "no-steam"))
    (tmp_path / "home").mkdir()


# ---------------------------------------------------------------------------
# 1. A stale mapping says where it went
# ---------------------------------------------------------------------------


def test_a_stale_mapping_names_the_path_it_left_behind(tmp_path):
    """Silently dropping it makes the error read like nothing was mapped."""
    gone = tmp_path / "moved-away"
    gone.mkdir()
    (gone / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n", "collection_revision": 1,
        "game_domain": DOMAIN, "mods": {},
    }))
    HandlerConfig().save()  # ensure the file exists
    cfg = HandlerConfig.load()
    cfg.mappings[DOMAIN] = [str(gone)]
    cfg.save()

    # now the collection moves away, leaving the mapping pointing at nothing
    import shutil
    shutil.rmtree(gone)

    with pytest.raises(NxmRoutingError) as exc:
        resolve_collection_dir(DOMAIN)

    msg = str(exc.value)
    assert str(gone) in msg, f"the stale path was not named: {msg}"
    assert "handler map" in msg or "unmap" in msg, msg


def test_a_mapping_pointing_at_a_non_collection_is_reported_too(tmp_path):
    """A directory that exists but is not a synced collection is the same case."""
    not_a_coll = tmp_path / "just-a-folder"
    not_a_coll.mkdir()
    cfg = HandlerConfig.load()
    cfg.mappings[DOMAIN] = [str(not_a_coll)]
    cfg.save()

    with pytest.raises(NxmRoutingError) as exc:
        resolve_collection_dir(DOMAIN)

    assert str(not_a_coll) in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# 2. `add`'s dependency warning is honest about what it can see
# ---------------------------------------------------------------------------


def test_no_requirements_carried_means_no_warning_not_an_all_clear():
    """Silence is right; inventing a warning would be worse.

    The REST mod endpoint carries no requirements field at all, so `add`
    must not claim to have checked dependencies it never saw.
    """
    assert _requirements_from_mod_info({"name": "x", "version": "1"}) == []
    assert _requirements_from_mod_info(None) == []


def test_requirements_that_are_carried_are_checked():
    """And when they are carried, the check is real."""
    assert _requirements_from_mod_info({"requirements": [72307]}) == [72307]
    assert _requirements_from_mod_info(
        {"requirements": [{"modId": "72307"}]}
    ) == [72307]
    assert _requirements_from_mod_info(
        {"modRequirements": {"nexusRequirements": {"nodes": [{"modId": 72307}]}}}
    ) == [72307]

    assert missing_requirements(_empty_state(), [72307]) == [72307]
    assert missing_requirements(_empty_state(), []) == []


def _empty_state():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / ".nexus-state.json").write_text(json.dumps({
        "collection_url": "u", "collection_name": "n", "collection_revision": 1,
        "game_domain": DOMAIN, "mods": {},
    }))
    st = CollectionState(d)
    st.load()
    return st


# ---------------------------------------------------------------------------
# 3. get_mod_requirements: the GraphQL lookup that makes the warning fire
# ---------------------------------------------------------------------------


def test_get_mod_requirements_returns_the_graphql_list(monkeypatch):
    """The GraphQL query is the only source of a standalone mod's needs."""
    from nexus_collection_dl.api import NexusAPI

    api = NexusAPI(api_key="k")
    calls = []

    def fake_graphql(query, variables=None):
        calls.append(query)
        if "game(" in query:
            return {"game": {"id": 1704}}
        return {"mod": {"modRequirements": {"nexusRequirements": {
            "nodes": [{"modId": "72307"}, {"modId": "12345"}],
        }}}}

    monkeypatch.setattr(api, "graphql_query", fake_graphql)
    result = api.get_mod_requirements("skyrimspecialedition", 74184)
    assert result == [72307, 12345]
    assert len(calls) == 2  # gameId lookup + mod query


def test_get_mod_requirements_stays_silent_when_the_game_is_unknown(monkeypatch):
    from nexus_collection_dl.api import NexusAPI

    api = NexusAPI(api_key="k")
    monkeypatch.setattr(api, "graphql_query", lambda q, v=None: {"game": None})
    assert api.get_mod_requirements("nosuchgame", 1) == []


def test_add_fires_the_warning_from_graphql(monkeypatch, tmp_path):
    """End to end: GraphQL supplies the requirement, the warning lands."""
    from unittest.mock import MagicMock, patch

    from nexus_collection_dl import service as service_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    state = _empty_state()
    state.save()

    svc = service_module.ModManagerService(api_key='k')
    svc._api = MagicMock()
    svc._api.get_mod_requirements.return_value = [72307]
    svc._api.get_mod_info.return_value = {"name": "X", "version": "1"}
    svc._api.get_mod_files.return_value = [
        {"file_id": 1, "name": "x.zip", "file_name": "x.zip", "version": "1"}
    ]
    svc._api.validate_key.return_value = {"is_premium": True}

    # Mock the download to return a result and the archive check to skip FOMOD
    with patch.object(service_module, "Downloader") as mock_dl, \
         patch.object(service_module, "_fomod_config_for_archive", return_value=None), \
         patch.object(service_module, "is_archive", return_value=True), \
         patch.object(service_module, "extract_archive", return_value=[state.mods_dir / "x.esp"]):
        mock_dl.return_value.download_mods.return_value = [
            ("mod", tmp_path / "x.zip")
        ]
        result = svc.add_mod(
            "https://www.nexusmods.com/skyrimspecialedition/mods/9",
            state.mods_dir,
        )
    assert any("72307" in w for w in result.warnings), result.warnings
