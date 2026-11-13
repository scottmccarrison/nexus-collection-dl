"""Unit tests for the /status health helper and the local scan helpers.

Every host probe is faked: no xdg-mime, gsettings, notify-send, or
network call ever runs here. The one real-lock test takes an in-process
flock and reads /proc/locks (a file read, no subprocess). Every fixture
secret is fake and asserted absent from outputs.
"""

import json
from pathlib import Path

import pytest

from nexus_collection_dl import service as service_module
from nexus_collection_dl.service import (
    CollectionScan,
    CollectionSummary,
    HealthCheck,
    HealthProbes,
    SkippedCollection,
    collect_health,
    collection_local_details,
    collection_manifest_mod_ids,
    list_collections,
    scan_collections,
)
from nexus_collection_dl.state import (
    LOCK_FILENAME,
    STATE_FILENAME,
    CollectionState,
    StateError,
)

GAME = "skyrimspecialedition"
COLLECTION_URL = f"https://next.nexusmods.com/{GAME}/collections/testcol"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def write_state(cdir: Path, name="Test Collection", mods=(), url=COLLECTION_URL):
    """Create a synced collection state file under cdir."""
    cdir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(cdir)
    state.set_collection_info(url, name, 1, GAME)
    for m in mods:
        state.add_mod(m)
    state.save()
    return cdir


def mod_entry(file_id=456, mod_id=123, name="Test Mod",
              download_status="downloaded"):
    return {
        "mod_id": mod_id,
        "mod_name": name,
        "file_id": file_id,
        "filename": f"{name.replace(' ', '')}-{file_id}.zip",
        "version": "1.0",
        "optional": False,
        "manual": False,
        "download_status": download_status,
    }


def fake_probes(**overrides):
    """Deterministic HealthProbes: no host access, nothing to warn about."""
    fields = dict(
        api_key_source=lambda: (None, None),
        handler_status=lambda: {
            "entry_installed": True,
            "is_default": True,
            "current_default": "nexus-dl-nxm.desktop",
            "xdg_mime_available": True,
        },
        notifications_enabled=lambda: None,
        notification_setting=lambda: None,
        capability_warnings=lambda: [],
        lock_state=lambda d: "free",
    )
    fields.update(overrides)
    return HealthProbes(**fields)


def empty_scan() -> CollectionScan:
    return CollectionScan(collections=[], skipped=[])


def run_checks(**kwargs) -> list[HealthCheck]:
    probes = kwargs.pop("probes", None) or fake_probes()
    scan = kwargs.pop("scan", None) or empty_scan()
    return collect_health(Path("/nonexistent"), scan=scan, probes=probes,
                          **kwargs)


def by_label(checks, label):
    return [c for c in checks if c.label == label]


# ---------------------------------------------------------------------------
# scan_collections / list_collections / collection_local_details
# ---------------------------------------------------------------------------

def test_scan_collections_reports_skipped_with_names_and_reasons(tmp_path):
    write_state(tmp_path / "Good", name="Good",
                mods=[mod_entry(file_id=1, mod_id=10)])
    broken = tmp_path / "Broken"
    broken.mkdir()
    (broken / STATE_FILENAME).write_text("{ not json")

    scan = scan_collections(tmp_path)
    assert [c.name for c in scan.collections] == ["Good"]
    assert [s.name for s in scan.skipped] == ["Broken"]
    assert scan.skipped[0].path == broken
    # A short, secret-free diagnostic reason accompanies the name.
    assert scan.skipped[0].reason


def test_list_collections_keeps_the_silent_skip_contract(tmp_path):
    """list_collections still drops broken states without reporting them."""
    write_state(tmp_path / "Good", name="Good")
    broken = tmp_path / "Broken"
    broken.mkdir()
    (broken / STATE_FILENAME).write_text("{ not json")

    assert [c.name for c in list_collections(tmp_path)] == ["Good"]


def test_collection_local_details_reports_counts_and_metadata(tmp_path):
    cdir = write_state(
        tmp_path / "Pack",
        name="My Pack",
        mods=[
            mod_entry(file_id=1, mod_id=10, name="A"),
            mod_entry(file_id=2, mod_id=20, name="B",
                      download_status="pending_download"),
        ],
    )
    state = CollectionState(cdir)
    state.load()
    state.mark_removed(99, "Gone Mod")
    state.save()

    local = collection_local_details(cdir)
    assert local.name == "My Pack"
    assert local.game_domain == GAME
    assert local.mod_count == 2
    assert local.pending_count == 1
    assert local.tombstone_count == 1
    assert local.collection_url == COLLECTION_URL
    assert local.state_path == cdir / STATE_FILENAME


# ---------------------------------------------------------------------------
# health: API key
# ---------------------------------------------------------------------------

def test_api_key_source_lines():
    checks = run_checks(probes=fake_probes(
        api_key_source=lambda: ("stored", None)))
    assert by_label(checks, "API key") == [
        HealthCheck("API key", "OK", "stored (keystore)")
    ]

    checks = run_checks(probes=fake_probes(
        api_key_source=lambda: ("env", None)))
    (line,) = by_label(checks, "API key")
    assert line.status == "OK"
    assert "NEXUS_API_KEY" in line.detail

    # A key passed to the session (flag) is reported without a source.
    checks = run_checks(api_key_present=True, probes=fake_probes(
        api_key_source=lambda: (None, None)))
    (line,) = by_label(checks, "API key")
    assert (line.status, line.detail) == ("OK", "passed to this session")

    # Absent: a WARN with a remedy.
    checks = run_checks(probes=fake_probes(
        api_key_source=lambda: (None, None)))
    (line,) = by_label(checks, "API key")
    assert line.status == "WARN"
    assert "not set" in line.detail
    assert "handler set-api-key" in line.remedy

    # Unreadable key store: a WARN naming the failure, not a crash.
    checks = run_checks(probes=fake_probes(
        api_key_source=lambda: (None, "could not read the key file")))
    (line,) = by_label(checks, "API key")
    assert line.status == "WARN"
    assert "could not be read" in line.detail


def test_api_key_probe_discards_the_value(monkeypatch):
    """The default probe returns the source only, never the key."""
    monkeypatch.setenv("NEXUS_API_KEY", "SUPERSECRETKEYVALUE")
    source, error = service_module._probe_api_key_source()
    assert source == "env"
    assert error is None
    assert "SUPERSECRETKEYVALUE" not in f"{source!r} {error!r}"


# ---------------------------------------------------------------------------
# health: nxm handler
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "info, status, detail_part",
    [
        ({"entry_installed": False, "is_default": False,
          "current_default": None, "xdg_mime_available": True},
         "WARN", "not installed"),
        ({"entry_installed": True, "is_default": True,
          "current_default": "nexus-dl-nxm.desktop",
          "xdg_mime_available": True},
         "OK", "installed and the default nxm:// handler"),
        ({"entry_installed": True, "is_default": False,
          "current_default": "other.desktop", "xdg_mime_available": True},
         "WARN", "not the default"),
        ({"entry_installed": True, "is_default": False,
          "current_default": None, "xdg_mime_available": True},
         "WARN", "no default nxm:// handler is registered"),
        ({"entry_installed": True, "is_default": False,
          "current_default": None, "xdg_mime_available": False},
         "OK", "xdg-mime not available"),
    ],
)
def test_handler_health_lines(info, status, detail_part):
    checks = run_checks(probes=fake_probes(handler_status=lambda: info))
    (line,) = by_label(checks, "nxm handler")
    assert line.status == status
    assert detail_part in line.detail


def test_handler_warn_lines_carry_a_remedy():
    checks = run_checks(probes=fake_probes(handler_status=lambda: {
        "entry_installed": False, "is_default": False,
        "current_default": None, "xdg_mime_available": True,
    }))
    (line,) = by_label(checks, "nxm handler")
    assert "nexus-dl handler install" in line.remedy


# ---------------------------------------------------------------------------
# health: notifications
# ---------------------------------------------------------------------------

CINNAMON = (
    "org.cinnamon.desktop.notifications",
    "display-notifications",
    "gsettings set org.cinnamon.desktop.notifications"
    " display-notifications true",
)


def test_notifications_unknown_desktop_is_silent():
    checks = run_checks(probes=fake_probes(
        notification_setting=lambda: None,
        notifications_enabled=lambda: False,
    ))
    assert by_label(checks, "Notifications") == []


def test_notifications_unreadable_switch_is_silent():
    checks = run_checks(probes=fake_probes(
        notification_setting=lambda: CINNAMON,
        notifications_enabled=lambda: None,
    ))
    assert by_label(checks, "Notifications") == []


def test_notifications_enabled_is_ok():
    checks = run_checks(probes=fake_probes(
        notification_setting=lambda: CINNAMON,
        notifications_enabled=lambda: True,
    ))
    (line,) = by_label(checks, "Notifications")
    assert line.status == "OK"


def test_notifications_disabled_warns_with_the_exact_fix():
    checks = run_checks(probes=fake_probes(
        notification_setting=lambda: CINNAMON,
        notifications_enabled=lambda: False,
    ))
    (line,) = by_label(checks, "Notifications")
    assert line.status == "WARN"
    assert "display-notifications is false" in line.detail
    assert line.remedy == CINNAMON[2]


# ---------------------------------------------------------------------------
# health: desktop capabilities
# ---------------------------------------------------------------------------

def test_capabilities_clean_is_one_ok_line():
    checks = run_checks(probes=fake_probes(
        capability_warnings=lambda: []))
    (line,) = by_label(checks, "Desktop tools")
    assert line.status == "OK"


def test_capabilities_warnings_render_as_warn_lines():
    checks = run_checks(probes=fake_probes(
        capability_warnings=lambda: ["notify-send was not found"]))
    (line,) = by_label(checks, "Desktop tools")
    assert line.status == "WARN"
    assert "notify-send" in line.detail


def test_capabilities_drops_the_notification_switch_warning():
    """The gsettings finding belongs to the Notifications check alone."""
    checks = run_checks(probes=fake_probes(
        notification_setting=lambda: CINNAMON,
        capability_warnings=lambda: [
            f"notifications are disabled ... run {CINNAMON[2]}",
            "notify-send was not found",
        ],
    ))
    lines = by_label(checks, "Desktop tools")
    assert len(lines) == 1
    assert "notify-send" in lines[0].detail
    assert CINNAMON[2] not in lines[0].detail


# ---------------------------------------------------------------------------
# health: state files
# ---------------------------------------------------------------------------

def test_state_files_ok_line_counts_readable_collections():
    scan = CollectionScan(
        collections=[
            CollectionSummary("A", GAME, 1, 0, Path("/tmp/a")),
            CollectionSummary("B", GAME, 1, 0, Path("/tmp/b")),
        ],
        skipped=[],
    )
    (line,) = by_label(run_checks(scan=scan), "State files")
    assert line.status == "OK"
    assert "2 collection state file(s) readable" in line.detail


def test_state_files_warning_names_the_skipped_ones():
    scan = CollectionScan(
        collections=[CollectionSummary("A", GAME, 1, 0, Path("/tmp/a"))],
        skipped=[SkippedCollection(
            "Broken Dir", Path("/tmp/Broken Dir"),
            "ValueError: bad key",
        )],
    )
    (line,) = by_label(run_checks(scan=scan), "State files")
    assert line.status == "WARN"
    assert "1 unreadable state file(s)" in line.detail
    assert "Broken Dir" in line.detail
    assert line.remedy


# ---------------------------------------------------------------------------
# health: state locks
# ---------------------------------------------------------------------------

def test_locks_free_is_ok_with_the_staleness_note():
    scan = CollectionScan(
        collections=[CollectionSummary("A", GAME, 1, 0, Path("/tmp/a"))],
        skipped=[],
    )
    (line,) = by_label(run_checks(scan=scan), "State locks")
    assert line.status == "OK"
    assert "no collection state lock is in use" in line.detail
    assert "stale lock files cannot exist" in line.detail


def test_locks_held_is_a_running_operation_not_stale():
    scan = CollectionScan(
        collections=[
            CollectionSummary("A", GAME, 1, 0, Path("/tmp/a")),
            CollectionSummary("B", GAME, 1, 0, Path("/tmp/b")),
        ],
        skipped=[],
    )
    (line,) = by_label(run_checks(
        scan=scan,
        probes=fake_probes(lock_state=lambda d: "held"),
    ), "State locks")
    assert line.status == "OK"
    assert "running operations (not stale)" in line.detail


def test_locks_all_unknown_reports_the_skip():
    scan = CollectionScan(
        collections=[CollectionSummary("A", GAME, 1, 0, Path("/tmp/a"))],
        skipped=[],
    )
    (line,) = by_label(run_checks(
        scan=scan,
        probes=fake_probes(lock_state=lambda d: "unknown"),
    ), "State locks")
    assert line.status == "OK"
    assert "skipped" in line.detail


def test_locks_partially_unknown_notes_the_gap():
    scan = CollectionScan(
        collections=[
            CollectionSummary("A", GAME, 1, 0, Path("/tmp/a")),
            CollectionSummary("B", GAME, 1, 0, Path("/tmp/b")),
        ],
        skipped=[],
    )
    (line,) = by_label(run_checks(
        scan=scan,
        probes=fake_probes(lock_state=lambda d: "unknown"),
    ), "State locks")
    assert "(1 not checked)" not in line.detail  # all unknown -> skip form

    calls = {"n": 0}

    def one_unknown(d):
        calls["n"] += 1
        return "unknown" if calls["n"] == 1 else "free"

    (line,) = by_label(run_checks(
        scan=scan,
        probes=fake_probes(lock_state=one_unknown),
    ), "State locks")
    assert "(1 not checked)" in line.detail


# ---------------------------------------------------------------------------
# health: guard rails
# ---------------------------------------------------------------------------

def test_guarded_probe_failure_renders_one_error_line_and_keeps_the_rest():
    def boom():
        raise RuntimeError("probe exploded")

    checks = run_checks(probes=fake_probes(
        handler_status=boom,
        notification_setting=lambda: CINNAMON,
        notifications_enabled=lambda: True,
    ))
    (line,) = by_label(checks, "nxm handler")
    assert line.status == "ERROR"
    assert "check failed (RuntimeError" in line.detail
    # The other checks still ran.
    labels = [c.label for c in checks]
    assert "API key" in labels
    assert "State locks" in labels
    # All six checks are present even with one failing.
    assert len(checks) == 6


def test_collect_health_returns_all_six_checks_in_order():
    labels = [c.label for c in run_checks(probes=fake_probes(
        notification_setting=lambda: CINNAMON,
        notifications_enabled=lambda: True,
    ))]
    assert labels == [
        "API key", "nxm handler", "Notifications",
        "Desktop tools", "State files", "State locks",
    ]


# ---------------------------------------------------------------------------
# the real lock probe (read-only /proc/locks; no subprocess)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not Path("/proc/locks").exists(), reason="needs /proc/locks"
)
def test_lock_state_sees_a_held_flock_in_process(tmp_path):
    import fcntl
    import os

    cdir = tmp_path / "c"
    cdir.mkdir()
    lock = cdir / LOCK_FILENAME
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert service_module._lock_state(cdir) == "free"
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            assert service_module._lock_state(cdir) == "held"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        assert service_module._lock_state(cdir) == "free"
    finally:
        os.close(fd)
    # No lock file at all: free, and a missing dir is not an error.
    assert service_module._lock_state(tmp_path / "nope") == "free"


# ---------------------------------------------------------------------------
# scan reason safety and manifest membership
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [" ", "", "\n\t "])
def test_scan_collections_survives_blank_exception_text(
    tmp_path, monkeypatch, message
):
    """A whitespace-only exception message must never turn the skip
    handler into an IndexError (it would hide every collection for
    list_collections' callers too, since list_collections delegates)."""
    write_state(tmp_path / "Good", name="Good")

    class BrokenState:
        def __init__(self, cdir):
            pass

        def load(self):
            raise ValueError(message)

    monkeypatch.setattr(service_module, "CollectionState", BrokenState)
    scan = scan_collections(tmp_path)
    assert scan.collections == []
    assert [s.name for s in scan.skipped] == ["Good"]
    assert scan.skipped[0].reason == "ValueError"
    assert list_collections(tmp_path) == []


def test_collection_manifest_mod_ids_reports_membership(tmp_path):
    cdir = write_state(tmp_path / "Pack", mods=[mod_entry()])
    state = CollectionState(cdir)
    state.load()
    state.manifest_data = {
        "mod_rules": [
            {"source": {"modId": 123, "fileID": 456}},
            {"reference": {"modID": 777, "fileID": 888}},
            "junk",
            {"source": {"modId": "not-a-number"}},
        ],
        "mod_phases": {"123": 2, "999": 0},
    }
    state.save()
    # Union of the mod_rules sides and the phase keys; junk is skipped.
    assert collection_manifest_mod_ids(cdir) == {123, 777, 999}


def test_collection_manifest_mod_ids_without_a_manifest(tmp_path):
    cdir = write_state(tmp_path / "Pack", mods=[mod_entry()])
    assert collection_manifest_mod_ids(cdir) == set()


def test_collection_manifest_mod_ids_raises_on_unreadable_state(tmp_path):
    cdir = tmp_path / "Broken"
    cdir.mkdir()
    (cdir / STATE_FILENAME).write_text("{ not json")
    with pytest.raises(StateError):
        collection_manifest_mod_ids(cdir)


# ---------------------------------------------------------------------------
# Round B: non-string names at the scan/list layer, bad state shapes
# ---------------------------------------------------------------------------

def test_scan_collections_coerces_non_string_names(tmp_path):
    write_state(tmp_path / "Numeric", name=42)
    write_state(tmp_path / "Blank", name=[])

    scan = scan_collections(tmp_path)
    # A truthy non-string name is coerced (no AttributeError in the
    # sort); a falsy one falls back to the directory name as before.
    assert {c.name for c in scan.collections} == {"42", "Blank"}
    # The silent delegate works the same.
    assert {c.name for c in list_collections(tmp_path)} == {"42", "Blank"}


@pytest.mark.parametrize("bad_shape", [[], "nope", 42])
def test_removed_mods_bad_shape_raises_state_error(tmp_path, bad_shape):
    """A present-but-wrong removed_mods shape must raise, never be
    silently dropped: dropping it would resurrect removed mods on the
    next save."""
    cdir = write_state(tmp_path / "Pack")
    path = cdir / STATE_FILENAME
    data = json.loads(path.read_text())
    data["removed_mods"] = bad_shape
    path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        CollectionState(cdir).load()


def test_removed_mods_absent_still_loads(tmp_path):
    """States written before tombstones existed load unchanged."""
    cdir = write_state(tmp_path / "Pack")
    path = cdir / STATE_FILENAME
    data = json.loads(path.read_text())
    del data["removed_mods"]
    path.write_text(json.dumps(data))
    state = CollectionState(cdir)
    state.load()
    assert state.removed_mods == {}


def test_mods_bad_shape_raises_state_error(tmp_path):
    cdir = write_state(tmp_path / "Pack")
    path = cdir / STATE_FILENAME
    data = json.loads(path.read_text())
    data["mods"] = []
    path.write_text(json.dumps(data))
    with pytest.raises(StateError):
        CollectionState(cdir).load()
