"""No raw exception text reaches a result string.

Result ``errors``/``warnings``/``error`` are a diagnostic record the
deploy report renders and ``web/app.py`` serializes to a browser. They
carry the deliberate part (which path, which refusal reason) and must
not carry arbitrary exception text, which can include absolute
filesystem paths and reflected request detail.

Two complementary checks:

- a runtime sweep that drives the deploy path with a hostile exception
  and asserts on every string field of the result, and
- a static scan of the package source for any exception interpolation
  that does not route through ``exception_reason``. That catches a
  missed site mechanically, without needing to drive every code path.
"""

from __future__ import annotations

import errno
import re
from pathlib import Path

from nexus_collection_dl.deploy import DeployError
from nexus_collection_dl.errmsg import exception_reason
from nexus_collection_dl.extractor import ExtractionError
from nexus_collection_dl.state import StateError

PKG = Path(__file__).resolve().parent.parent / "nexus_collection_dl"

# Substrings that must never appear in any result string.
FORBIDDEN = ("/home/scott", "id_rsa", "DEADBEEF", "[Errno")

HOSTILE_OS = OSError(
    errno.ENOENT, "No such file or directory", "/home/scott/.ssh/id_rsa"
)
HOSTILE_VAL = ValueError("api key is DEADBEEF leaked")


# --- the classifier --------------------------------------------------------


def test_os_error_becomes_phrase_plus_errno_name():
    exc = FileNotFoundError(errno.ENOENT, "No such file or directory",
                            "/home/scott/.ssh/id_rsa")
    assert exception_reason(exc) == "no such file or directory (ENOENT)"


def test_os_error_drops_the_filename():
    reason = exception_reason(
        OSError(errno.EACCES, "Permission denied", "/home/scott/secret")
    )
    assert reason == "permission denied (EACCES)"
    assert "secret" not in reason
    assert "/home" not in reason


def test_unlisted_errno_falls_back_to_strerror():
    assert exception_reason(
        OSError(errno.ELOOP, "Too many symbolic links", "/x")
    ) == "too many symbolic links (ELOOP)"


def test_unknown_errno_uses_strerror_without_the_filename():
    reason = exception_reason(OSError(99999, "weird thing", "/home/scott/secret"))
    assert "99999" in reason
    assert "secret" not in reason


def test_os_error_without_errno_is_a_bare_category():
    assert exception_reason(OSError("device exploded")) == "I/O error"


def test_outside_exception_collapses_to_class_name():
    reason = exception_reason(HOSTILE_VAL)
    assert reason == "unexpected error (ValueError)"
    assert "DEADBEEF" not in reason


def test_package_exception_message_passes_through():
    # Authored here and bounded by construction; see the errmsg docstring.
    assert exception_reason(DeployError("not a regular file or symlink")) == \
        "not a regular file or symlink"
    assert exception_reason(StateError("collection state is not usable")) == \
        "collection state is not usable"
    assert exception_reason(ExtractionError("archive is corrupt")) == \
        "archive is corrupt"


def test_os_error_category_is_not_str_repr():
    exc = OSError(errno.ENOENT, "No such file or directory", "/p")
    assert exception_reason(exc) != str(exc)
    assert ": '/p'" not in exception_reason(exc)


# --- the runtime sweep -----------------------------------------------------


def _assert_clean(result) -> None:
    fields: list[str] = []
    for name in ("errors", "warnings"):
        value = getattr(result, name, None)
        if isinstance(value, list):
            fields.extend(str(v) for v in value)
    value = getattr(result, "error", None)
    if isinstance(value, str) and value:
        fields.append(value)
    assert fields, f"{type(result).__name__} carried no text to check"
    for text in fields:
        for forbidden in FORBIDDEN:
            assert forbidden not in text, (
                f"{type(result).__name__} leaked {forbidden!r} in {text!r}"
            )


def _write_collection(cdir: Path, name="Sweep", mods=()):
    from nexus_collection_dl.state import CollectionState

    cdir.mkdir(parents=True, exist_ok=True)
    state = CollectionState(cdir)
    state.set_collection_info(
        "https://next.nexusmods.com/skyrimspecialedition/collections/x",
        name, 1, "skyrimspecialedition",
    )
    for m in mods:
        state.add_mod(m)
    state.save()
    return cdir


def _service(monkeypatch):
    from nexus_collection_dl.service import ModManagerService

    monkeypatch.setattr(
        "nexus_collection_dl.service.NexusAPI", lambda *a, **kw: object()
    )
    return ModManagerService(api_key="fake")


def test_deploy_errors_do_not_leak_hostile_oserror(monkeypatch, tmp_path):
    """The real deploy path, with install_deploy raising a hostile OSError."""
    import nexus_collection_dl.deploy as deploy_mod

    mods = tmp_path / "coll"
    _write_collection(mods)
    (mods / "Data").mkdir()
    (mods / "Data" / "a.esp").write_bytes(b"x")
    game = tmp_path / "game"
    game.mkdir()

    monkeypatch.setattr(
        deploy_mod, "install_deploy",
        lambda *a, **kw: (_ for _ in ()).throw(HOSTILE_OS),
    )
    result = _service(monkeypatch).deploy(mods, game_dir=game)
    _assert_clean(result)


def test_service_except_exception_sites_do_not_leak(monkeypatch, tmp_path):
    """The `except Exception` collectors in service.py must classify too.

    Those are the sites that would otherwise swallow anything at all.
    """
    import nexus_collection_dl.service as service_mod

    mods = tmp_path / "coll"
    _write_collection(mods, mods=[{
        "mod_id": 1, "mod_name": "M", "file_id": 2, "filename": "M.zip",
        "version": "1.0", "optional": False, "manual": False,
        "download_status": "downloaded",
    }])
    monkeypatch.setattr(
        service_mod.ModManagerService, "_regen_load_order_from_state",
        lambda *a, **kw: (_ for _ in ()).throw(HOSTILE_VAL),
    )
    result = _service(monkeypatch).remove_mod(mods, file_id=2)
    _assert_clean(result)


# --- the static scan -------------------------------------------------------
#
# The runtime sweep can only drive the paths it knows. The static scan
# covers the whole package: any exception interpolation that does not
# route through exception_reason is a missed site.


# An f-string placeholder bound to an exception variable. Matches the
# bare forms ({e}, {err}, {exc}, {save_exc}); a placeholder with
# attribute access or a comprehension is deliberate and checked by the
# site itself.
_EXC_PLACEHOLDER = re.compile(
    r"\{(e|err|exc|ex|save_exc|exception|cause)\}"
)

# str(e) and friends, which are the other way raw text gets in.
_EXC_STR = re.compile(r"\bstr\((e|err|exc|ex|save_exc|exception)\)")

# Lines that legitimately carry raw exception text. All of these are
# rendering surfaces that redact or truncate on the way out, not result
# builders: the CLI and shell error paths, the boundary redactor, the
# handler's message sanitizer, and the --json envelope (whose
# error.message is the documented diagnostic surface).
_ALLOWED = (
    "exception_reason(",
    "redact_secret_values(",
    "_boundary_redact(",
    "sanitize_message(",
    "console.print",
    "progress.console.print",
    "_emit_error(",
    "_emit_failure(",
    "_scrub_failure(",
    "_scrub_error(",
    # jsonout builds the --json envelope; its error.message is the
    # documented place for diagnostic text (see the skill's errors.md).
    "jsonout",
)


def test_no_exception_interpolation_escapes_exception_reason():
    """Every exception placeholder in the package is inside exception_reason(...).

    A bare ``f"...{e}"`` in a raise or an errors.append is exactly the
    leak #82 is about; the fix routes it through exception_reason.
    """
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "errmsg.py":
            continue
        # jsonout builds the --json envelope; its error.message is the
        # documented place for diagnostic text (see the skill's errors.md).
        if path.name == "jsonout.py":
            continue
        lines = path.read_text().splitlines()
        for lineno, line in enumerate(lines):
            if not (_EXC_PLACEHOLDER.search(line) or _EXC_STR.search(line)):
                continue
            if any(a in line for a in _ALLOWED):
                continue
            # An argument on its own line belongs to the call above it,
            # which is where the rendering helper name lives.
            if lineno > 0 and any(a in lines[lineno - 1] for a in _ALLOWED):
                continue
            # A set/dict comprehension bound to a loop variable of the
            # same name is not an exception.
            if re.search(r"for\s+\w+\s+in\s+", line):
                continue
            offenders.append(f"{path.name}:{lineno + 1}: {line.strip()}")
    assert offenders == [], (
        "exception text interpolated without exception_reason:\n"
        + "\n".join(offenders)
    )


def test_web_serializers_do_not_str_a_result():
    """The web fallback must be a category, not str(task.result)."""
    app_src = (PKG / "web" / "app.py").read_text()
    tasks_src = (PKG / "web" / "tasks.py").read_text()
    assert "str(task.result)" not in _code_only(app_src)
    assert "str(data)" not in _code_only(tasks_src)
    assert "internal error" in app_src
    assert "internal error" in tasks_src


def _code_only(src: str) -> str:
    """Source with comments stripped, so a docstring mention is not a hit."""
    out = []
    for line in src.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  #")[0])
    return "\n".join(out)
