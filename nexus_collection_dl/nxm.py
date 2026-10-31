"""nxm:// protocol link parsing and validation.

nxm:// links are what the Nexus Mods website hands to mod managers. Two
shapes exist:

    nxm://{game}/mods/{mod_id}/files/{file_id}[?key=...&expires=...&user_id=...]
    nxm://{game}/collections/{slug}[/revisions/{n|latest}]   (Vortex collections)

Individual file links can carry download credentials: a ``key``/``expires``
pair the website issues for the logged-in account. Those values authenticate
one download and are treated as secrets. This module never echoes them in
error messages, reprs, or logs, and the parser is deliberately strict so a
crafted link cannot smuggle unexpected structure (userinfo, ports,
fragments, traversal segments, duplicate or malformed credential
parameters) into the download flow.

The credential pair is only ever redeemed, never minted: the API request
forwards ``key``/``expires`` exactly as the website issued them (decoded
once), and ``user_id`` is a client-side identity check only, never an API
query parameter.
"""

import re
import time
from dataclasses import dataclass
from urllib.parse import unquote

# Bound the input so a giant URI cannot be used to burn CPU or memory.
MAX_NXM_URI_LENGTH = 8192

_GAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")
_FILE_PATH_RE = re.compile(r"^/mods/([1-9][0-9]{0,11})/files/([1-9][0-9]{0,11})$")
_COLLECTION_PATH_RE = re.compile(
    r"^/collections/([^/?#]+)(?:/revisions/(latest|[1-9][0-9]{0,9}))?$"
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_INT_RE = re.compile(r"^[1-9][0-9]{0,11}$")
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
# Decoded key tokens: printable ASCII, bounded length. (An '&' can never
# survive the query split; '=' is allowed for base64 padding.)
_KEY_CHARSET_RE = re.compile(r"^[!-~]{1,512}$")
_SCHEME_RE = re.compile(r"^[nN][xX][mM]://")
# Leading scheme token of a non-nxm input (e.g. "https:"). The charset is
# strict, so echoing the matched token back in an error is always safe.
_SCHEME_TOKEN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")

_GENERIC_ERROR = (
    "Unrecognized nxm:// link. Expected "
    "nxm://{game}/mods/{mod_id}/files/{file_id}, optionally with "
    "key/expires download credentials."
)


class NxmParseError(Exception):
    """Raised when an nxm:// link cannot be parsed.

    Messages are built from safe components only: they never include the
    raw URI, the credential key, or the expires value.
    """


class NxmSchemeError(NxmParseError):
    """Raised when the input carries a non-nxm scheme (e.g. an https:// URL).

    The message explains that the link type is not usable here and how
    to get an nxm:// link instead. Only the scheme token, matched from
    a strict charset, is echoed; host, path, and query of the input are
    never repeated, so a credential-bearing query string can never leak
    through this error.
    """

    def __init__(self, scheme: str):
        self.scheme = scheme
        super().__init__(
            f"This is a {scheme}:// link, not an nxm:// link, so this "
            "link type is not usable with 'nexus-dl nxm'.\n"
            "The 'Mod manager download' button on a Nexus Mods file page "
            "is what produces an nxm:// link: right-click it and choose "
            "copy link address to get the link for manual use.\n"
            "To make that button hand links to nexus-dl directly, run "
            "'nexus-dl handler install' once."
        )


class NxmCollectionLinkError(NxmParseError):
    """Raised for collection-shaped nxm:// links (unsupported by this tool).

    Carries safe sync guidance built from validated components, never the
    raw URI or an unvalidated URL.
    """

    def __init__(self, game_domain: str, slug: str, revision: str, web_url: str):
        self.game_domain = game_domain
        self.slug = slug
        self.revision = revision
        self.web_url = web_url
        guidance = (
            "nexus-dl does not download collections from nxm:// links. "
            "Use the collection URL instead:\n"
            f'  nexus-dl sync "{web_url}" <mods-dir>\n'
            f"This link points at revision '{revision}' of the collection; "
            "sync always installs the latest published revision."
        )
        super().__init__(guidance)


@dataclass
class NxmLinkInfo:
    """Parsed individual-file nxm:// link.

    ``key`` and ``expires`` are website-issued download credentials. The
    repr hides both so a stray debug print, log record, or exception
    context can never leak them.
    """

    game_domain: str
    mod_id: int
    file_id: int
    key: str | None = None
    expires: int | None = None
    user_id: int | None = None

    def __repr__(self) -> str:
        return (
            f"NxmLinkInfo(game_domain={self.game_domain!r}, "
            f"mod_id={self.mod_id}, file_id={self.file_id}, "
            f"key={'[redacted]' if self.key is not None else None!r}, "
            f"expires={'[redacted]' if self.expires is not None else None!r}, "
            f"user_id={self.user_id})"
        )

    @property
    def has_credentials(self) -> bool:
        """True when the link carries a complete key/expires pair."""
        return self.key is not None and self.expires is not None

    def is_expired(self, now: float | None = None) -> bool:
        """Local expiry check for the credential pair (no network)."""
        if self.expires is None:
            return False
        return self.expires <= (time.time() if now is None else now)

    def redacted_uri(self) -> str:
        """Safe printable form: identity only, never credentials."""
        return f"nxm://{self.game_domain}/mods/{self.mod_id}/files/{self.file_id}"


def _validate_percent_encoding(uri: str) -> None:
    """Reject malformed percent escapes before any decoding happens."""
    i = uri.find("%")
    while i != -1:
        if not _PERCENT_RE.match(uri, i):
            raise NxmParseError("nxm:// link contains invalid percent encoding.")
        i = uri.find("%", i + 3)


def _decode_once(value: str, what: str) -> str:
    """Percent-decode exactly once, rejecting invalid UTF-8 sequences."""
    try:
        return unquote(value, errors="strict")
    except UnicodeDecodeError:
        raise NxmParseError(f"nxm:// link has an invalid percent-encoded {what}.")


def _parse_collection(game_domain: str, path: str) -> "NxmLinkInfo":
    """Recognize the Vortex collection shape and turn it into safe guidance."""
    match = _COLLECTION_PATH_RE.match(path)
    # Only build a website URL from a slug that validates after decoding;
    # anything else falls back to the generic parse error.
    try:
        slug = _decode_once(match.group(1), "collection slug")
    except NxmParseError:
        raise NxmParseError(_GENERIC_ERROR)
    if not _SLUG_RE.match(slug):
        raise NxmParseError(_GENERIC_ERROR)
    revision = match.group(2) or "latest"
    web_url = f"https://next.nexusmods.com/{game_domain}/collections/{slug}"
    raise NxmCollectionLinkError(game_domain, slug, revision, web_url)


def parse_nxm_url(uri: str) -> NxmLinkInfo:
    """Parse and validate an nxm:// individual-file link.

    Strict by design. Rejects: wrong scheme, oversized input, whitespace
    and control characters, invalid percent encoding, userinfo, ports,
    fragments, malformed or traversal paths, non-positive or oversized ids,
    duplicate credential parameters (the website never sends them, and two
    values for one credential is an ambiguity we refuse to guess at),
    malformed key/expires/user_id values, and a half-present credential
    pair. Unknown query parameters are ignored without being logged.

    Raises NxmCollectionLinkError (a NxmParseError) for collection links,
    carrying safe sync guidance instead.
    """
    if not isinstance(uri, str) or not uri:
        raise NxmParseError(_GENERIC_ERROR)
    if len(uri) > MAX_NXM_URI_LENGTH:
        raise NxmParseError(
            f"nxm:// link is too long (more than {MAX_NXM_URI_LENGTH} characters)."
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in uri):
        raise NxmParseError("nxm:// link contains control characters.")
    if any(c.isspace() for c in uri):
        raise NxmParseError("nxm:// link contains whitespace.")

    # Scheme detection runs before percent validation: a non-nxm input
    # (an https:// mod-page URL, say) gets the wrong-link-type guidance
    # even when it also contains malformed percent escapes. Nothing is
    # decoded before the scheme matches, so strictness is unchanged.
    scheme = _SCHEME_RE.match(uri)
    if not scheme:
        token = _SCHEME_TOKEN_RE.match(uri)
        if token and token.group(1).lower() != "nxm":
            raise NxmSchemeError(token.group(1).lower())
        raise NxmParseError(_GENERIC_ERROR)
    _validate_percent_encoding(uri)
    rest = uri[scheme.end():]

    if "#" in rest:
        raise NxmParseError("nxm:// link contains a fragment, which is not supported.")

    if "?" in rest:
        authority_path, query = rest.split("?", 1)
    else:
        authority_path, query = rest, None

    slash = authority_path.find("/")
    if slash == -1:
        netloc, path = authority_path, ""
    else:
        netloc, path = authority_path[:slash], authority_path[slash:]

    if not netloc:
        raise NxmParseError(_GENERIC_ERROR)
    if "@" in netloc:
        raise NxmParseError("nxm:// link contains userinfo, which is not supported.")
    if ":" in netloc:
        raise NxmParseError("nxm:// link contains a port, which is not supported.")
    game_domain = netloc.lower()
    if not _GAME_RE.match(game_domain):
        raise NxmParseError("nxm:// link has an invalid game domain.")

    if _COLLECTION_PATH_RE.match(path):
        return _parse_collection(game_domain, path)

    file_match = _FILE_PATH_RE.match(path)
    if not file_match:
        raise NxmParseError(_GENERIC_ERROR)
    mod_id = int(file_match.group(1))
    file_id = int(file_match.group(2))

    key: str | None = None
    expires: int | None = None
    user_id: int | None = None
    if query is not None:
        seen: set[str] = set()
        for segment in query.split("&"):
            if not segment:
                raise NxmParseError("nxm:// link has an empty query segment.")
            if "=" not in segment:
                raise NxmParseError("nxm:// link has a malformed query parameter.")
            raw_name, raw_value = segment.split("=", 1)
            # Decode names too, so an encoded duplicate ('%6bey=...') cannot
            # slip a second credential value past the ambiguity check.
            name = _decode_once(raw_name, "query parameter").lower()
            if name not in ("key", "expires", "user_id"):
                continue  # harmless extra parameter: ignored, never logged
            if name in seen:
                raise NxmParseError(
                    f"nxm:// link has duplicate '{name}' parameters; refusing "
                    "to guess which value is intended."
                )
            seen.add(name)
            value = _decode_once(raw_value, f"'{name}' value")
            if name == "key":
                if not _KEY_CHARSET_RE.match(value):
                    raise NxmParseError("nxm:// link has an invalid download key.")
                key = value
            elif name == "expires":
                if not _INT_RE.match(value):
                    raise NxmParseError("nxm:// link has an invalid 'expires' value.")
                expires = int(value)
            else:
                if not _INT_RE.match(value):
                    raise NxmParseError("nxm:// link has an invalid 'user_id' value.")
                user_id = int(value)

    if (key is None) != (expires is None):
        raise NxmParseError(
            "nxm:// link credential parameters 'key' and 'expires' must appear "
            "together (or not at all)."
        )

    return NxmLinkInfo(
        game_domain=game_domain,
        mod_id=mod_id,
        file_id=file_id,
        key=key,
        expires=expires,
        user_id=user_id,
    )
