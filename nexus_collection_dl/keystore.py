"""Machine-local API key store for browser-launched downloads.

A desktop-launched ``nexus-dl nxm`` process (an nxm:// click) inherits
the desktop session environment, not the user's shell, so a
``NEXUS_API_KEY`` exported only in ``.bashrc`` never reaches it. This
module stores the key under the XDG config directory
(``~/.config/nexus-dl/api-key`` unless ``XDG_CONFIG_HOME`` says
otherwise) so click-through downloads authenticate without a daemon and
without shell environment variables.

Secret hygiene: the CLI reads the key from hidden stdin, the file is
written atomically with 0600 permissions, and no command ever prints
the value or embeds it in an exception, repr, or log message.
set/remove/status report only the stored state and the file path. Key
resolution precedence is defined here and in exactly one place:
explicit ``--api-key`` flag > ``NEXUS_API_KEY`` env > stored key >
none (the keyless free/pending flow stays unchanged).
"""

import contextlib
import os
import re
import tempfile
from pathlib import Path

from .handler import config_dir

API_KEY_FILENAME = "api-key"
ENV_VAR_NAME = "NEXUS_API_KEY"

# Plausibility bounds for a stored key, validated locally (no network).
# Nexus personal API keys are long opaque strings; these checks catch
# paste mistakes (empty input, stray whitespace, truncated or oversized
# pastes) without enforcing a specific key format.
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 512
_PLAUSIBLE_KEY_RE = re.compile(r"^[!-~]+$")  # printable ASCII, no spaces

# Resolution source labels reported by 'handler status'.
SOURCE_FLAG = "flag"
SOURCE_ENV = "env"
SOURCE_STORED = "stored"


class KeyStoreError(Exception):
    """Raised when a stored API key cannot be read, written, or removed.

    Messages carry paths and OS-level detail only, never key values.
    """


def api_key_path() -> Path:
    """Location of the stored key file (XDG-aware, next to handler.json)."""
    return config_dir() / API_KEY_FILENAME


def validate_api_key(key: str) -> str:
    """Validate a candidate API key locally; return the cleaned value.

    Surrounding whitespace is stripped (a copy/paste artifact). The
    result must be non-empty, printable ASCII without spaces, and within
    the length bounds. No network validation happens here. Error
    messages describe the problem without echoing the value.
    """
    value = str(key).strip()
    if not value:
        raise KeyStoreError("The API key is empty.")
    if len(value) < MIN_KEY_LENGTH:
        raise KeyStoreError(
            f"The API key looks too short to be valid ({len(value)} "
            "characters); Nexus API keys are long strings. Nothing was "
            "stored."
        )
    if len(value) > MAX_KEY_LENGTH:
        raise KeyStoreError(
            f"The input is too long to be an API key ({len(value)} "
            "characters). Nothing was stored."
        )
    if not _PLAUSIBLE_KEY_RE.match(value):
        raise KeyStoreError(
            "The API key contains whitespace or non-printable characters, "
            "so the paste looks malformed. Nothing was stored."
        )
    return value


def _atomic_write_secret(path: Path, content: str) -> None:
    """Write content to path atomically with 0600 permissions.

    Same temp-file + fsync + rename pattern as the handler config, plus
    an explicit fchmod so the secret file's permission guarantee never
    depends on temp-file defaults. The config directory is created 0700
    when missing; permissions of an existing directory are left alone.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as e:
        raise KeyStoreError(
            f"Could not create config directory {path.parent}: "
            f"{e.strerror or e}"
        )
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as e:
        raise KeyStoreError(f"Could not write {path}: {e.strerror or e}")
    try:
        with os.fdopen(fd, "w") as f:
            # fchmod is absolute (umask does not apply), so the secret
            # file is owner-read/write only no matter how the process
            # was started.
            os.fchmod(f.fileno(), 0o600)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise KeyStoreError(f"Could not write {path}: {e.strerror or e}")


def store_api_key(key: str) -> Path:
    """Validate and store the API key; return the path written.

    The write is atomic and 0600; a previously stored key is replaced.
    The value never appears in any exception this function raises.
    """
    value = validate_api_key(key)
    path = api_key_path()
    _atomic_write_secret(path, value + "\n")
    return path


def load_api_key() -> str | None:
    """Return the stored key, or None when no key is set.

    A missing or empty file means "not set". A file that exists but
    cannot be read is a KeyStoreError instead of a silent None, so a
    broken store can never masquerade as a keyless setup. Error text
    carries the path and OS detail only, never a value.
    """
    path = api_key_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise KeyStoreError(
            f"Could not read the stored API key at {path}: "
            f"{e.strerror or e}. Fix the file, or run 'nexus-dl handler "
            "remove-api-key' and store the key again."
        )
    value = raw.strip()
    return value or None


def remove_api_key() -> bool:
    """Delete the stored key file; return True if it existed.

    Only the key file is removed. The rest of the config directory
    (game mappings, the desktop record) is left untouched.
    """
    path = api_key_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise KeyStoreError(f"Could not remove {path}: {e.strerror or e}")
    return True


def resolve_api_key(explicit: str | None = None) -> tuple[str | None, str | None]:
    """Resolve the effective API key and where it came from.

    Single home of the precedence rule: an explicit ``--api-key`` value
    wins, then the ``NEXUS_API_KEY`` environment variable, then the
    stored key. Empty or whitespace-only values at any level fall
    through to the next. When no level provides a key the result is
    ``(None, None)`` and the existing keyless behavior applies.

    Returns ``(key, source)`` with source ``"flag"``, ``"env"``,
    ``"stored"``, or None. Raises KeyStoreError when a stored key
    exists but cannot be read.
    """
    if explicit is not None and explicit.strip():
        return explicit, SOURCE_FLAG
    env_value = os.environ.get(ENV_VAR_NAME)
    if env_value is not None and env_value.strip():
        return env_value, SOURCE_ENV
    stored = load_api_key()
    if stored is not None:
        return stored, SOURCE_STORED
    return None, None
