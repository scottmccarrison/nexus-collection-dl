"""Shared credential redaction for user-visible output boundaries.

One implementation of the boundary-redaction pipeline, used by both the
``nexus-dl nxm`` error boundary (cli.py) and the interactive shell
(shell.py) so neither re-implements it. The pipeline has two layers:

* literal secret values are removed in raw, case-varied, and
  percent-encoded shapes (api.redact_literal_values /
  api.redact_encoded_secret_forms), and
* URI-shaped text is stripped (handler.sanitize_message) so a signed
  link can never be echoed back to the user.

A caller builds a secret scope (an API key plus link query values, see
uri_query_secrets) and passes it with the text to boundary_redact.
Secrets are never displayed or logged here; they are only used as
replacement patterns.
"""

import re
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from .api import redact_encoded_secret_forms, redact_literal_values
from .handler import sanitize_message

# Floor for BARE-LITERAL prose replacement at typed-error boundaries
# only. Real API keys and link credentials are long opaque strings (the
# key store enforces the same 8-character minimum), while replacing a
# one-character literal would mangle ordinary guidance words like
# "link". This exemption NEVER applies to percent-encoded runs or to
# occurrences inside URI query strings, where a short credential cannot
# be ordinary prose. Accepted residual risk (see boundary_redact): a
# short-but-valid credential reflected bare in prose survives
# typed-branch redaction; the catch-all branch applies no floor.
MIN_REDACT_SECRET_LEN = 8

# Any-scheme URI, for confining short-secret bare-literal redaction to
# query strings.
_ANY_URI_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")


def uri_query_secrets(uri: str) -> tuple[str, ...]:
    """Credential values carried in a URI query string.

    Query extraction mirrors the nxm parser's acceptance rules rather
    than parse_qs: parameter names are percent-decoded once and
    case-folded (so 'KEY=' and '%6bey=' are both the 'key' parameter),
    and values are percent-decoded once while a literal '+' stays a '+'
    (parse_qs would turn it into a space, putting a value that is not
    the real credential in scope and leaving the real one out). Both the
    raw and the decoded value join the scope. Values are never
    displayed, only used for replacement.
    """
    base: list[str] = []
    try:
        query = urlsplit(uri).query
    except ValueError:
        query = ""
    for segment in query.split("&"):
        if not segment or "=" not in segment:
            continue
        raw_name, raw_value = segment.split("=", 1)
        if unquote(raw_name).lower() not in ("key", "expires"):
            continue
        for value in (raw_value, unquote(raw_value)):
            if value:
                base.append(value)
    return tuple(base)


def uri_tokens(text: str) -> tuple[str, ...]:
    """Every URI-shaped token in text (any scheme, with or without query)."""
    return tuple(_ANY_URI_RE.findall(str(text)))


def text_uri_secrets(text: str) -> tuple[str, ...]:
    """Query credentials carried by every URI-shaped token in text.

    Convenience for callers that redact free-form input lines: the
    scope covers each token's key/expires query values (see
    uri_query_secrets for the extraction rules).
    """
    values: list[str] = []
    for token in uri_tokens(text):
        values.extend(uri_query_secrets(token))
    return tuple(values)


def merge_secrets(*groups: Iterable[Any]) -> tuple:
    """Flatten secret groups into one de-duplicated scope of non-empty
    string values.

    Empty and whitespace-only values are dropped: they would match
    everywhere in re.sub-based redaction and mangle unrelated prose
    (redact_literal_values skips them for the same reason). Order is
    preserved (first occurrence wins) so rendered output is
    deterministic across runs.
    """
    unique: list = []
    for group in groups:
        for value in group:
            text = str(value)
            if not text or text.isspace():
                continue
            if text not in unique:
                unique.append(text)
    return tuple(unique)


def redact_secrets_in_uri_queries(text: str, secrets: tuple) -> str:
    """Bare-literal redaction of short scoped secrets inside URI queries.

    Only the part after the first '?' of each URI is touched; URIs
    without a query are left alone. Inside a query string even a short
    credential cannot be ordinary prose, so the length floor does not
    apply there.
    """

    def scrub(match: "re.Match[str]") -> str:
        uri = match.group(0)
        head, sep, query = uri.partition("?")
        if not sep:
            return uri
        for value in secrets:
            query = re.sub(
                re.escape(value), "[redacted]", query, flags=re.IGNORECASE
            )
        return head + sep + query

    return _ANY_URI_RE.sub(scrub, text)


def boundary_redact(
    text: str,
    secrets: tuple,
    *,
    strip_uris: bool = True,
    min_secret_len: int = 0,
) -> str:
    """Full redaction scope for text leaving through an output boundary.

    Every scoped secret (API key, link key/expires in raw and decoded
    form) is redacted in raw and percent-encoded shapes. When
    ``min_secret_len`` is set (typed domain-error branches), secrets
    shorter than it are exempted from bare-literal prose replacement
    only: they are still redacted in percent-encoded runs and inside
    URI query strings. Typed branches also skip URI stripping because
    their messages are redacted at the source and may deliberately
    carry a redacted API URL or the nxm:// scheme name in guidance.
    """
    long_secrets = tuple(s for s in secrets if len(s) >= min_secret_len)
    short_secrets = tuple(s for s in secrets if len(s) < min_secret_len)
    out = redact_literal_values(text, long_secrets)
    if short_secrets:
        out = redact_encoded_secret_forms(out, short_secrets)
        out = redact_secrets_in_uri_queries(out, short_secrets)
    return sanitize_message(out) if strip_uris else out
