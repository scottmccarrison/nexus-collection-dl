"""Tests for ModManagerService.import_downloads browser-download matching.

Everything runs against temporary directories and state files. Archive
extraction is mocked (or uses stdlib zip only), so no Nexus API access,
network calls, or secrets are involved.
"""

import hashlib
import zipfile
from pathlib import Path

import pytest

from nexus_collection_dl import service as service_module
from nexus_collection_dl.extractor import ExtractionError
from nexus_collection_dl.service import ModManagerService
from nexus_collection_dl.state import CollectionState


def _make_zip(path: Path, inner_name: str = "plugin.esp", payload: bytes = b"data") -> Path:
    """Create a real zip archive (valid magic bytes for is_archive)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner_name, payload)
    return path


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _write_state(
    mods_dir: Path,
    pending: list[dict],
    manifest_data: dict | None = None,
) -> CollectionState:
    """Write a state file with the given pending downloads."""
    mods_dir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(mods_dir)
    state.set_collection_info(
        "https://next.nexusmods.com/skyrimspecialedition/collections/test",
        "Test Collection",
        1,
        "skyrimspecialedition",
    )
    state.manifest_data = manifest_data
    for entry in pending:
        state.add_mod({
            "file_id": entry["file_id"],
            "mod_id": entry.get("mod_id", 0),
            "mod_name": entry.get("mod_name", f"Mod {entry['file_id']}"),
            "filename": entry.get("filename", ""),
            "version": entry.get("version", ""),
            "download_status": "pending_download",
            "browser_url": entry.get("browser_url", ""),
        })
    state.save()
    return state


def _reload(mods_dir: Path) -> CollectionState:
    state = CollectionState(mods_dir)
    state.load()
    return state


def _manifest_with_md5(mod_id: int, file_id: int, logical: str, md5: str) -> dict:
    """Minimal cached-manifest shape with one mod_rules MD5 entry."""
    return {
        "mod_rules": [
            {
                "source": {
                    "modId": mod_id,
                    "fileID": file_id,
                    "logicalFileName": logical,
                    "fileMD5": md5,
                },
                "reference": {"type": "include", "logicalFileName": logical},
            }
        ],
        "plugins": [],
        "plugin_rules": [],
        "mod_phases": {},
        "logical_name_to_mod_id": {},
        "mod_choices": {},
    }


@pytest.fixture()
def svc() -> ModManagerService:
    # The API key is never used: import_downloads must not touch the network
    return ModManagerService(api_key="test-key-unused")


@pytest.fixture()
def extract_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Mock service.extract_archive and record (archive, target) calls."""
    calls: list[tuple[Path, Path]] = []

    def fake_extract(archive_path, target_dir):
        calls.append((Path(archive_path), Path(target_dir)))
        return []

    monkeypatch.setattr(service_module, "extract_archive", fake_extract)
    return calls


@pytest.fixture()
def md5_counter(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap service._file_md5 to record which files get hashed."""
    calls: list[Path] = []
    real_md5 = service_module._file_md5

    def counting_md5(path):
        calls.append(Path(path))
        return real_md5(path)

    monkeypatch.setattr(service_module, "_file_md5", counting_md5)
    return calls


# -- exact filename and hash precedence --


def test_exact_full_name_match(tmp_path, svc, extract_recorder):
    archive = _make_zip(tmp_path / "Immersive Armor-1234-1-0-1699999999.zip")
    _make_zip(tmp_path / "Unrelated-9999-2-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Immersive Armor",
        "filename": "Immersive Armor-1234-1-0-1699999999.zip", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert result.extracted == 1
    assert result.errors == []
    assert extract_recorder == [(archive, tmp_path)]
    assert not archive.exists()  # removed after successful extract
    state = _reload(tmp_path)
    assert state.mods[10].download_status == "downloaded"
    assert state.mods[10].browser_url == ""


def test_real_zip_extraction_end_to_end(tmp_path, svc):
    """One test exercises the real (stdlib) extraction path, no mocks."""
    archive = _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip", inner_name="mod/plugin.esp")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert result.extracted == 1
    assert (tmp_path / "mod" / "plugin.esp").exists()
    assert not archive.exists()


def test_exact_filename_beats_hash_match(tmp_path, svc, extract_recorder):
    """Exact known archive filename wins; a hash hit must not clobber it."""
    exact = _make_zip(tmp_path / "Foo.zip", payload=b"not-the-manifest-content")
    _make_zip(tmp_path / "mislabeled.bin", payload=b"manifest-content")
    manifest = _manifest_with_md5(
        1234, 10, "Foo.zip", _md5(tmp_path / "mislabeled.bin")
    )
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo.zip", "version": "1.0",
    }], manifest_data=manifest)

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(exact, tmp_path)]


def test_hash_match_when_file_renamed(tmp_path, svc, extract_recorder):
    """Verified content hash wins over a decoy with a matching stem."""
    renamed = _make_zip(tmp_path / "renamed-download.bin", payload=b"manifest-content")
    _make_zip(tmp_path / "Foo.zip", payload=b"wrong-content")
    manifest = _manifest_with_md5(
        1234, 10, "Foo-1234-1-0-1699999999.zip", _md5(renamed)
    )
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }], manifest_data=manifest)

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(renamed, tmp_path)]


def test_identical_duplicate_content_resolves_by_hash(tmp_path, svc, extract_recorder):
    """Byte-identical re-downloads are content-verified, so one is taken."""
    src = _make_zip(tmp_path / "src.zip", payload=b"same-bytes")
    data = src.read_bytes()
    (tmp_path / "Foo.zip").write_bytes(data)
    (tmp_path / "Foo (1).zip").write_bytes(data)
    src.unlink()
    manifest = _manifest_with_md5(1234, 10, "Anything.zip", _md5(tmp_path / "Foo.zip"))
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }], manifest_data=manifest)

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert result.errors == []
    assert len(extract_recorder) == 1  # exactly one duplicate consumed
    assert _reload(tmp_path).mods[10].download_status == "downloaded"


# -- browser suffix and logical names --


def test_browser_suffix_match(tmp_path, svc, extract_recorder):
    archive = _make_zip(tmp_path / "Immersive Armor-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Immersive Armor",
        "filename": "Immersive Armor", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(archive, tmp_path)]


def test_browser_suffix_compound_version(tmp_path, svc, extract_recorder):
    """Compound versions with hyphens ('1.0.3' -> '1-0-3') still parse."""
    archive = _make_zip(tmp_path / "Mod-1234-1-0-3-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Mod",
        "filename": "Mod", "version": "1.0.3",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(archive, tmp_path)]


def test_logical_name_matches_archive_stem(tmp_path, svc, extract_recorder):
    """GraphQL file.name often lacks the extension: 'Foo' matches 'Foo.7z'."""
    archive = _make_zip(tmp_path / "Foo.7z")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(archive, tmp_path)]


def test_case_insensitive_exact_match(tmp_path, svc, extract_recorder):
    archive = _make_zip(tmp_path / "FOO.ZIP")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "foo.zip", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(archive, tmp_path)]


def test_unknown_mod_id_refuses_suffix_match(tmp_path, svc, extract_recorder):
    """Without mod identity there is no suffix match, only a pending error."""
    _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 0, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert result.still_pending == 1
    assert len(result.errors) == 1
    assert extract_recorder == []


# -- conservative rejection --


def test_short_names_do_not_fuzzy_match(tmp_path, svc, extract_recorder):
    """The old word regex matched loose overlaps; identity matching must not."""
    _make_zip(tmp_path / "uitools-8888-1-0-1699999999.zip")
    _make_zip(tmp_path / "Better Armors-777-1-0-1699999999.zip")
    _write_state(tmp_path, [
        {"file_id": 10, "mod_id": 9999, "mod_name": "ui", "filename": "ui",
         "version": "1.0", "browser_url": "https://www.nexusmods.com/skyrimspecialedition/mods/9999"},
        {"file_id": 11, "mod_id": 8888, "mod_name": "Armor", "filename": "Armor",
         "version": "2.0"},
    ])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert result.still_pending == 2
    assert len(result.errors) == 2
    assert "Download it from https://www.nexusmods.com/skyrimspecialedition/mods/9999" in result.errors[0]
    assert extract_recorder == []


def test_version_mismatch_does_not_match(tmp_path, svc, extract_recorder):
    _make_zip(tmp_path / "Mod-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Mod",
        "filename": "Mod", "version": "2.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert result.still_pending == 1
    assert len(result.errors) == 1
    assert extract_recorder == []


def test_ambiguous_stem_match_stays_pending(tmp_path, svc, extract_recorder):
    _make_zip(tmp_path / "Foo.zip", payload=b"one")
    _make_zip(tmp_path / "Foo.7z", payload=b"two")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert result.still_pending == 1
    assert "Ambiguous" in result.errors[0]
    assert "Foo.7z" in result.errors[0] and "Foo.zip" in result.errors[0]
    assert extract_recorder == []
    assert (tmp_path / "Foo.zip").exists() and (tmp_path / "Foo.7z").exists()
    assert _reload(tmp_path).mods[10].download_status == "pending_download"


def test_case_variant_files_are_ambiguous(tmp_path, svc, extract_recorder):
    """'Foo.zip' and 'foo.zip' may hold different content: never guess."""
    _make_zip(tmp_path / "Foo.zip", payload=b"one")
    _make_zip(tmp_path / "foo.zip", payload=b"two")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo.zip", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert "Ambiguous" in result.errors[0]
    assert extract_recorder == []


def test_one_file_fulfills_at_most_one_pending(tmp_path, svc, extract_recorder):
    """Two pending items matching the same file: first claims, second errors."""
    _make_zip(tmp_path / "Alpha-100-1-0-1699999999.zip")
    _write_state(tmp_path, [
        {"file_id": 10, "mod_id": 100, "mod_name": "Alpha Full",
         "filename": "Alpha-100-1-0-1699999999.zip", "version": "1.0"},
        {"file_id": 11, "mod_id": 100, "mod_name": "Alpha Display",
         "filename": "Alpha", "version": "1.0"},
    ])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert result.still_pending == 1
    assert any("already matched Alpha Full" in e for e in result.errors)
    state = _reload(tmp_path)
    assert state.mods[10].download_status == "downloaded"
    assert state.mods[11].download_status == "pending_download"


def test_extracted_remnant_files_do_not_match(tmp_path, svc, extract_recorder):
    """A loose extracted game file is not a download candidate."""
    (tmp_path / "Foo.pak").write_bytes(b"not an archive")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert result.still_pending == 1
    assert extract_recorder == []


# -- exclusions and lazy hashing --


def test_hidden_and_state_files_excluded(tmp_path, svc, extract_recorder, md5_counter):
    """Hidden files (state, dotfiles) are never matched or hashed."""
    _make_zip(tmp_path / ".Foo.zip")  # hidden decoy with a matching stem
    manifest = _manifest_with_md5(1234, 10, "Foo.zip", "0" * 32)
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }], manifest_data=manifest)

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 0
    assert result.still_pending == 1
    assert md5_counter == []  # nothing non-hidden to hash
    assert extract_recorder == []


def test_hashing_is_lazy_and_cached(tmp_path, svc, extract_recorder, md5_counter):
    # Part 1: no expected hashes anywhere -> zero hashing
    _make_zip(tmp_path / "A-1-1-0-1699999999.zip", payload=b"aaa")
    _make_zip(tmp_path / "B-2-1-0-1699999999.zip", payload=b"bbb")
    _write_state(tmp_path, [
        {"file_id": 1, "mod_id": 1, "mod_name": "A", "filename": "A", "version": "1.0"},
        {"file_id": 2, "mod_id": 2, "mod_name": "B", "filename": "B", "version": "1.0"},
    ])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 2  # both via browser suffix
    assert md5_counter == []

    # Part 2: expected hashes exist -> each unique candidate hashed once
    mods2 = tmp_path / "second"
    mods2.mkdir(parents=True, exist_ok=True)
    a2 = _make_zip(mods2 / "renamed-a.bin", payload=b"aaa")
    b2 = _make_zip(mods2 / "renamed-b.bin", payload=b"bbb")
    _make_zip(mods2 / "decoy.bin", payload=b"ccc")
    manifest = {
        "mod_rules": [
            {"source": {"modId": 1, "fileID": 1, "logicalFileName": "A.zip",
                        "fileMD5": _md5(a2)}},
            {"source": {"modId": 2, "fileID": 2, "logicalFileName": "B.zip",
                        "fileMD5": _md5(b2)}},
        ],
    }
    _write_state(mods2, [
        {"file_id": 1, "mod_id": 1, "mod_name": "A", "filename": "A", "version": "1.0"},
        {"file_id": 2, "mod_id": 2, "mod_name": "B", "filename": "B", "version": "1.0"},
    ], manifest_data=manifest)

    result2 = svc.import_downloads(mods2, no_load_order=True)

    assert result2.matched == 2
    assert len(md5_counter) == 3  # three unique files, hashed once each
    assert len(set(md5_counter)) == 3


# -- failure handling and lifecycle --


def test_extraction_failure_keeps_pending(monkeypatch, tmp_path, svc):
    def failing_extract(archive_path, target_dir):
        raise ExtractionError("corrupt archive")

    monkeypatch.setattr(service_module, "extract_archive", failing_extract)
    archive = _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert result.extracted == 0
    assert result.still_pending == 1
    assert any("Extraction error" in e for e in result.errors)
    assert archive.exists()  # not removed after failure
    assert _reload(tmp_path).mods[10].download_status == "pending_download"


def test_no_extract_marks_downloaded_and_keeps_file(tmp_path, svc, extract_recorder):
    archive = _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path, no_load_order=True, no_extract=True)

    assert result.matched == 1
    assert result.extracted == 0
    assert archive.exists()
    assert extract_recorder == []
    assert _reload(tmp_path).mods[10].download_status == "downloaded"


def test_repeat_import_is_noop(tmp_path, svc, extract_recorder):
    _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    first = svc.import_downloads(tmp_path, no_load_order=True)
    assert first.matched == 1

    second = svc.import_downloads(tmp_path, no_load_order=True)
    assert (second.matched, second.extracted, second.still_pending) == (0, 0, 0)
    assert second.errors == []


def test_malformed_manifest_does_not_crash(tmp_path, svc, extract_recorder):
    """Junk manifest entries are skipped; suffix matching still works."""
    archive = _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    manifest = {
        "mod_rules": [
            "not-a-dict",
            None,
            {"source": "not-a-dict"},
            {"source": {"modId": "abc", "fileID": None, "fileMD5": 123}},
            {"reference": {"modId": 1234, "fileID": 10,
                           "logicalFileName": "Foo-1234-1-0-1699999999.zip"}},
        ],
    }
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }], manifest_data=manifest)

    result = svc.import_downloads(tmp_path, no_load_order=True)

    assert result.matched == 1
    assert extract_recorder == [(archive, tmp_path)]


def test_default_load_order_path_without_manifest(tmp_path, svc, extract_recorder):
    """Default flags with no cached manifest: no crash, no load order files."""
    _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    result = svc.import_downloads(tmp_path)

    assert result.matched == 1
    assert result.load_order_files == []


def test_progress_callback_reused_and_no_tqdm(tmp_path, svc, extract_recorder):
    events: list[tuple[str, float, str]] = []
    _make_zip(tmp_path / "Foo-1234-1-0-1699999999.zip")
    _write_state(tmp_path, [{
        "file_id": 10, "mod_id": 1234, "mod_name": "Foo",
        "filename": "Foo", "version": "1.0",
    }])

    svc.import_downloads(
        tmp_path,
        no_load_order=True,
        on_progress=lambda e, p, m: events.append((e, p, m)),
    )

    kinds = [e for e, _, _ in events]
    assert kinds[0] == "import"
    assert kinds[-1] == "done"
    assert len(events) >= 3
    percentages = [p for _, p, _ in events]
    assert percentages == sorted(percentages)
    assert all(0.0 <= p <= 1.0 for p in percentages)
    # tqdm must be gone: progress goes through the existing callback only
    assert not hasattr(service_module, "tqdm")
