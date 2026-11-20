"""Bounded, safe text for exceptions embedded in result strings.

Result ``errors``/``warnings``/``error`` fields are a diagnostic record:
the deploy report renders them, and ``web/app.py`` serializes them
verbatim to a browser. They carry the deliberate part (which path, which
refusal reason) and must not carry arbitrary exception text. An
unexpected exception's message can include absolute filesystem paths and
reflected request detail, and the web UI is the one boundary with no
``boundary_redact`` layer.

This module is the single place that turns an exception into the short
category that goes in its place. It is not a logging helper and not the
web error boundary (``web/errors.py::client_error_message`` handles
raised exceptions; this handles text already inside a result).
"""

import errno as _errno
import os

# Fixed English phrases for the errnos that actually occur here. A map
# rather than os.strerror so the text is stable across locales and
# platforms; os.strerror is only the fallback for an unlisted errno.
_OS_PHRASES = {
    _errno.ENOENT: "no such file or directory",
    _errno.EACCES: "permission denied",
    _errno.EPERM: "permission denied",
    _errno.ENOTDIR: "not a directory",
    _errno.EISDIR: "is a directory",
    _errno.ENOSPC: "no space left on device",
    _errno.EROFS: "read-only filesystem",
    _errno.EEXIST: "already exists",
    _errno.ELOOP: "too many symbolic links",
    _errno.ENAMETOOLONG: "name too long",
    _errno.EBUSY: "device or resource busy",
    _errno.EIO: "input/output error",
}


def exception_reason(exc: BaseException) -> str:
    """Short safe category for an exception. Never raw exception text.

    OSError becomes the fixed OS phrase plus the symbolic errno name
    (``no such file or directory (ENOENT)``), with no filename and no
    OS-provided detail: the path is already in the authored part of the
    message around this call, and ``str(OSError)`` appends it again.

    An exception raised by this package passes through as ``str(exc)``.
    Those messages are authored here and bounded by construction. That
    pass-through is only sound because every ``{e}`` interpolation inside
    the package's own raises also routes through this function; a missed
    one re-leaks, which is why tests/test_error_text.py sweeps every
    service entry point with a hostile exception.

    Anything from outside the package collapses to a bare category plus
    the class name (a class name is our own code, not user or API data),
    because an outside exception can carry anything at all.
    """
    if isinstance(exc, OSError):
        return _os_reason(exc)

    mod = type(exc).__module__ or ""
    if mod == "nexus_collection_dl" or mod.startswith("nexus_collection_dl."):
        return str(exc)
    return f"unexpected error ({type(exc).__name__})"


def _os_reason(exc: OSError) -> str:
    """Fixed phrase plus symbolic errno name, with no path or detail."""
    code = exc.errno
    if code is None:
        return "I/O error"
    phrase = _OS_PHRASES.get(code)
    if phrase is None:
        try:
            phrase = os.strerror(code)
        except (ValueError, OverflowError):
            phrase = "I/O error"
    name = _errno.errorcode.get(code)
    return f"{phrase} ({name})" if name else phrase
