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
