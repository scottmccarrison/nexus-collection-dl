"""Machine-readable JSON output for the CLI.

One envelope for every command so a harness has a single parse target:

    {"ok": true,  "result": {...}, "error": null}
    {"ok": false, "result": null,  "error": {"code", "type", "message", "hint"}}

`ok` is true exactly when the process exits 0. A partial failure (a result
that reports its own errors, like `AddResult.success is False`) sets
`ok` to false with `error.code == "operation_failed"` while still populating
`result`, so a caller gets both the outcome and the detail.

Everything here writes to stdout and nothing else does, so `json.loads` over
the whole of stdout is always safe.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any


# Error codes, most-derived first. This is an ordered isinstance walk rather
# than a dict lookup because several of these subclass one another
# (StateBusyError is a StateError, UndeployError is a DeployError,
# NexusPremiumRequired is a NexusAPIError) and the more specific code must win.
def _build_error_codes() -> tuple[tuple[type[BaseException], str], ...]:
    # Imported here so importing this module never drags in the whole package.
    from .api import NexusAPIError, NexusPremiumRequired, NexusRateLimited
    from .collection import CollectionParseError, ModParseError
    from .deploy import DeployError, UndeployError
    from .downloader import DownloadError
    from .extractor import ExtractionError
    from .handler import DesktopEntryError, HandlerError, NxmRoutingError
    from .keystore import KeyStoreError
    from .loadorder import LoadOrderError
    from .manifest import ManifestError
    from .nxm import NxmCollectionLinkError, NxmParseError, NxmSchemeError
    from .state import LoadOrderRegenError, StateBusyError, StateError

    return (
        (StateBusyError, "state_busy"),
        (LoadOrderRegenError, "load_order_regen_error"),
        (StateError, "state_error"),
        (NexusPremiumRequired, "nexus_premium_required"),
        (NexusRateLimited, "nexus_rate_limited"),
        (NexusAPIError, "nexus_api_error"),
        (UndeployError, "undeploy_error"),
        (DeployError, "deploy_error"),
        (NxmCollectionLinkError, "nxm_collection_link_error"),
        (NxmSchemeError, "nxm_scheme_error"),
        (NxmParseError, "nxm_parse_error"),
        (NxmRoutingError, "nxm_routing_error"),
        (DesktopEntryError, "desktop_entry_error"),
        (HandlerError, "handler_error"),
        (DownloadError, "download_error"),
        (ExtractionError, "extraction_error"),
        (ManifestError, "manifest_error"),
        (LoadOrderError, "load_order_error"),
        (CollectionParseError, "collection_parse_error"),
        (ModParseError, "mod_parse_error"),
        (KeyStoreError, "key_store_error"),
        (ValueError, "invalid_argument"),
        (FileNotFoundError, "not_found"),
    )


_CODES: tuple[tuple[type[BaseException], str], ...] | None = None


def error_code_for(exc: BaseException) -> str:
    """Return the stable machine-readable code for an exception."""
    global _CODES
    if _CODES is None:
        _CODES = _build_error_codes()
    for exc_type, code in _CODES:
        if isinstance(exc, exc_type):
            return code
    # Names are code-defined, so this fallback is stable across releases.
    return type(exc).__name__


def to_jsonable(value: Any) -> Any:
    """Convert a service result to something json.dumps accepts.

    `dataclasses.asdict` leaves `Path` objects intact (SyncResult.collection_dir,
    CollectionSummary.path, NxmResult and friends), which `json.dumps`
    refuses. Coerce explicitly so every result serializes.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def _envelope(ok: bool, result: Any, error: Any) -> dict[str, Any]:
    return {"ok": ok, "result": to_jsonable(result), "error": to_jsonable(error)}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_result(result: Any) -> None:
    """Emit a successful envelope. `result` must be an object, never a bare list."""
    _write(_envelope(True, result, None))


def emit_failure(
    exc: BaseException,
    *,
    hint: str | None = None,
    result: Any = None,
    code: str | None = None,
) -> None:
    """Emit a failure envelope. `result` is kept when an operation partially completed."""
    error: dict[str, Any] = {
        "code": code or error_code_for(exc),
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if hint:
        error["hint"] = hint
    _write(_envelope(False, result, error))
