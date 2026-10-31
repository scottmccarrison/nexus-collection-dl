"""Nexus Mods API client for GraphQL and REST endpoints."""

import contextlib
import logging
import os
import re
import threading
import time
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

from . import __version__

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
REST_BASE_URL = "https://api.nexusmods.com/v1"

_URL_RE = re.compile(r"https?://\S+")

# (connect, read) seconds for the signed download-link request. It runs
# inside the serialized HTTP-logging context, so an unbounded hang would
# also stall every other thread's signed request; the timeout keeps that
# lock hold bounded. Other API endpoints keep their pre-existing
# (untimed) behavior for now.
SIGNED_REQUEST_TIMEOUT = (15, 60)


def redact_url(url: str) -> str:
    """Strip query and fragment from a URL for safe display.

    Signed download credentials live in query strings, so error messages
    and logs only ever carry scheme, host, and path.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "[url redacted]"
    if not parts.scheme and not parts.netloc and not parts.path:
        return "[url redacted]"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def redact_literal_values(text: str, secrets: Iterable[Any]) -> str:
    """Remove literal credential values from text.

    Defense in depth for error paths: a server that reflects a download
    key in an error body or a hostile response URL can never surface the
    credential. Every explicitly provided secret is redacted regardless
    of length (a caller-supplied credential is always worth protecting);
    only None, empty, and whitespace-only values are skipped so unrelated
    text is not mangled.
    """
    out = str(text)
    for secret in secrets:
        if secret is None:
            continue
        value = str(secret)
        if value and not value.isspace():
            out = out.replace(value, "[redacted]")
    return out


def redact_secret_values(text: str, secrets: Iterable[Any]) -> str:
    """Redact literal credential values, then strip any residual URLs.

    Used for free-form exception text (downloaders, network errors) where
    a URL with a signed query could appear anywhere in the message.
    """
    return _URL_RE.sub("[url redacted]", redact_literal_values(text, secrets))


# Serializes suppress_http_debug_logging contexts across threads. Logger
# levels are process-global, so two concurrent signed requests must never
# interleave "raise to INFO" with another request's "restore to DEBUG":
# the restore would re-expose the still-running request's signed URL to
# DEBUG handlers. The RLock is reentrant, so nested contexts in one thread
# (downloader inside API call) compose without deadlock; the inner context
# saves and restores the already-lifted levels, and only the outermost
# context restores the originals. Cross-thread contexts are fully
# serialized: no protected request is active when levels are restored.
_HTTP_LOG_LOCK = threading.RLock()


@contextlib.contextmanager
def suppress_http_debug_logging():
    """Keep DEBUG-level HTTP logging from writing signed URLs to logs.

    urllib3 logs request lines (including query strings) at DEBUG level,
    and download credentials live in query strings. The relevant loggers
    are lifted to INFO for the duration of a signed request and restored
    afterwards, so even an application running with DEBUG logging cannot
    leak a credential through them.

    Thread safety: the whole context (level lift, request, restore) runs
    under a module RLock. Overlapping signed requests in different threads
    are serialized, so a restore can never happen while another protected
    request is still running; nested same-thread contexts are reentrant
    and only the outermost restores the original levels. Restoration is
    exact even when the request raises. The lock is held only around
    signed operations and never modifies logging configuration outside
    the context, so the user's own logging setup is untouched.
    """
    with _HTTP_LOG_LOCK:
        loggers = [
            logging.getLogger(name)
            for name in ("urllib3", "requests", "urllib3.connectionpool")
        ]
        previous = [lg.level for lg in loggers]
        try:
            for lg in loggers:
                if lg.level < logging.INFO:
                    lg.setLevel(logging.INFO)
            yield
        finally:
            for lg, level in zip(loggers, previous):
                lg.setLevel(level)


class NexusAPIError(Exception):
    """Base exception for Nexus API errors."""
    pass


class NexusPremiumRequired(NexusAPIError):
    """Raised when Premium membership is required for an operation."""
    pass


class NexusRateLimited(NexusAPIError):
    """Raised when rate limited by the API."""

    def __init__(self, retry_after: int = 60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after} seconds.")


class NexusAPI:
    """Client for Nexus Mods API (GraphQL + REST)."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("NEXUS_API_KEY")
        if not self.api_key:
            raise NexusAPIError(
                "No API key provided. Set NEXUS_API_KEY environment variable "
                "or pass --api-key flag."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": self.api_key,
                "User-Agent": f"nexus-collection-dl/{__version__}",
            }
        )
        self._last_request_time = 0.0
        self._min_request_interval = 0.5  # 2 requests per second max

    def _rate_limit_wait(self) -> None:
        """Ensure we don't exceed rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    def _handle_response(
        self, response: requests.Response, secrets: tuple = ()
    ) -> Any:
        """Handle API response and raise appropriate errors.

        Every error path is credential-safe: messages never embed the
        response URL query (where signed download credentials live), the
        raw response body, or the text of requests' own HTTPError (which
        includes the full request URL). Known secret values are redacted
        from whatever text is included. The API key itself (resolved
        from the --api-key flag, the environment, or the machine-local
        key store) joins every redaction scope built here: it travels
        in a request header, and a hostile or compromised endpoint could
        reflect it back inside a URL or body.
        """
        status = response.status_code
        # redact_literal_values skips None/empty values, so a missing
        # key costs nothing here.
        redaction = tuple(secrets) + (self.api_key,)
        if 200 <= status < 300:
            try:
                return response.json()
            except ValueError:
                # A proxy, captive portal, or compromised endpoint can
                # return HTML with a 2xx status. The body and URL are
                # never echoed: they could carry reflected credentials.
                raise NexusAPIError(
                    redact_literal_values(
                        f"The Nexus API returned a non-JSON response "
                        f"(HTTP {status}); a proxy or network middleware "
                        "may be intercepting the request.",
                        redaction,
                    )
                )

        if status == 429:
            try:
                retry_after = int(response.headers.get("Retry-After", 60))
            except (TypeError, ValueError):
                retry_after = 60
            raise NexusRateLimited(retry_after)

        if status == 403:
            # The body is inspected privately for the premium hint but is
            # never included in the message: a hostile or compromised
            # endpoint could reflect credentials in it.
            premium = False
            try:
                data = response.json() if response.text else {}
                premium = "premium" in str(data).lower()
            except ValueError:
                premium = False
            if premium:
                raise NexusPremiumRequired(
                    "Premium membership required for direct download links."
                )
            raise NexusAPIError(
                redact_literal_values(
                    "Access forbidden by the Nexus API (HTTP 403).", redaction
                )
            )

        if status == 404:
            raise NexusAPIError(
                redact_literal_values(
                    f"Resource not found: {redact_url(response.url)}", redaction
                )
            )

        if status == 401:
            raise NexusAPIError(
                redact_literal_values(
                    "The Nexus API rejected the API key (HTTP 401). "
                    "Check NEXUS_API_KEY.",
                    redaction,
                )
            )

        if status == 400:
            raise NexusAPIError(
                redact_literal_values(
                    "The Nexus API rejected the request (HTTP 400).", redaction
                )
            )

        if status == 410:
            raise NexusAPIError(
                redact_literal_values(
                    "The requested resource is no longer available (HTTP 410).",
                    redaction,
                )
            )

        # Any other non-2xx: never propagate raise_for_status()'s text,
        # which embeds the full request URL including signed query values.
        raise NexusAPIError(
            redact_literal_values(
                f"The Nexus API returned an error (HTTP {status}).", redaction
            )
        )

    def graphql_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query."""
        self._rate_limit_wait()
        response = self.session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
        )
        data = self._handle_response(response)
        if "errors" in data:
            # Server-controlled text: a hostile or compromised endpoint
            # could reflect the apikey or a signed URL inside an error
            # entry. Redact known secrets and strip residual URLs before
            # the text is embedded in the exception (same defense as
            # every other error path).
            raise NexusAPIError(
                redact_secret_values(
                    f"GraphQL errors: {data['errors']}", (self.api_key,)
                )
            )
        return data.get("data", {})

    def get_collection_mods(self, game_domain: str, slug: str) -> dict[str, Any]:
        """
        Get collection metadata and ALL mod files from the latest revision.

        IMPORTANT: A collection can include multiple files from the same mod
        (e.g. a core pak + texture pack + compatibility patch, all from mod_id 18726).
        We return ALL files, keyed by file_id not mod_id, to avoid dropping any.

        Returns dict with:
            - name: collection name
            - revision: revision number
            - mods: list of mod file info dicts (one entry per FILE, not per mod)
        """
        query = """
        query GetCollection($slug: String!) {
            collection(slug: $slug) {
                id
                slug
                name
                summary
                latestPublishedRevision {
                    revisionNumber
                    downloadLink
                    modFiles {
                        fileId
                        optional
                        file {
                            fileId
                            name
                            version
                            sizeInBytes
                            mod {
                                modId
                                name
                                pictureUrl
                                modRequirements {
                                    nexusRequirements {
                                        nodes {
                                            modId
                                            modName
                                            notes
                                        }
                                        totalCount
                                    }
                                }
                            }
                        }
                    }
                }
                game {
                    domainName
                }
            }
        }
        """
        data = self.graphql_query(query, {"slug": slug})
        collection = data.get("collection")
        if not collection:
            raise NexusAPIError(f"Collection not found: {slug}")

        # Verify game domain matches
        actual_game = collection.get("game", {}).get("domainName", "")
        if actual_game.lower() != game_domain.lower():
            raise NexusAPIError(
                f"Game mismatch: URL says '{game_domain}' but collection is for '{actual_game}'"
            )

        revision = collection.get("latestPublishedRevision", {})
        download_link = revision.get("downloadLink")
        mod_files = revision.get("modFiles", [])

        mods = []
        seen_file_ids: set[int] = set()

        for mf in mod_files:
            file_info = mf.get("file", {})
            mod_info = file_info.get("mod", {})
            if not file_info or not mod_info:
                continue

            file_id = file_info.get("fileId")
            if file_id is None:
                continue

            # Skip truly duplicate file_ids (shouldn't happen but be safe)
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)

            # Extract mod requirements
            req_nodes = (
                mod_info.get("modRequirements", {})
                .get("nexusRequirements", {})
                .get("nodes", [])
            )
            requirements = [node["modId"] for node in req_nodes]

            mods.append(
                {
                    "mod_id": mod_info.get("modId"),
                    "mod_name": mod_info.get("name"),
                    "file_id": file_id,
                    "filename": file_info.get("name"),
                    "version": file_info.get("version"),
                    "size_bytes": file_info.get("sizeInBytes"),
                    "optional": mf.get("optional", False),
                    "requirements": requirements,
                }
            )

        return {
            "id": collection.get("id"),
            "slug": collection.get("slug"),
            "name": collection.get("name"),
            "summary": collection.get("summary"),
            "game_domain": actual_game,
            "revision": revision.get("revisionNumber"),
            "download_link": download_link,
            "mods": mods,
        }

    def get_download_url(
        self,
        game_domain: str,
        mod_id: int,
        file_id: int,
        key: str | None = None,
        expires: int | None = None,
    ) -> str:
        """Get direct download URL for a mod file.

        ``key``/``expires`` are website-issued download credentials (the
        pair an nxm:// link carries for free accounts). They are
        forwarded as query parameters exactly as issued, always as a
        pair, and are never minted here. A caller's ``user_id`` is an
        identity check done elsewhere and is never sent to this endpoint.
        Premium accounts may omit the pair entirely.

        Credential handling on error paths: the response URL query, the
        response body, and the credential values themselves are redacted
        from every exception message, and DEBUG HTTP logging is
        suppressed for the request so the signed query cannot leak
        through logs. Redirects are refused (allow_redirects=False) so
        the apikey header and the credential pair can never be forwarded
        to an arbitrary host. An invalid or expired pair is reported as
        an actionable failure; it never falls back to an unsigned
        request.
        """
        if (key is None) != (expires is None):
            raise NexusAPIError(
                "download_link requires the key/expires credential pair "
                "together (or neither)."
            )
        self._rate_limit_wait()
        url = (
            f"{REST_BASE_URL}/games/{game_domain}/mods/{mod_id}"
            f"/files/{file_id}/download_link.json"
        )
        params = {"key": key, "expires": expires} if key is not None else None
        # The API key joins the local redaction scope alongside the
        # signed pair: it is a secret wherever it could be echoed, and
        # redact_literal_values ignores None/empty values.
        secrets: tuple = (
            (key, expires, self.api_key) if key is not None else (self.api_key,)
        )

        with suppress_http_debug_logging():
            # allow_redirects=False: this request carries the apikey header
            # and the signed key/expires pair. Following a redirect would
            # forward them to an arbitrary host; a redirect is refused as a
            # safe failure instead (requests strips nothing custom). The
            # bounded timeout keeps the serialized HTTP-logging lock hold
            # bounded even if the endpoint stalls.
            response = self.session.get(
                url,
                params=params,
                allow_redirects=False,
                timeout=SIGNED_REQUEST_TIMEOUT,
            )

        try:
            data = self._handle_response(response, secrets=secrets)
        except (NexusPremiumRequired, NexusRateLimited):
            raise
        except NexusAPIError as e:
            if key is not None:
                raise NexusAPIError(
                    f"{e} The download credentials in this link were rejected "
                    "or have expired; click 'Mod manager download' on the "
                    "Nexus website again to get a fresh link."
                ) from None
            raise

        if not data or not isinstance(data, list) or not isinstance(data[0], dict):
            raise NexusAPIError(
                redact_literal_values(
                    "Unexpected download link response from the Nexus API.",
                    secrets,
                )
            )
        return data[0].get("URI")

    def get_mod_files(self, game_domain: str, mod_id: int) -> list[dict[str, Any]]:
        """List all files for a mod."""
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/games/{game_domain}/mods/{mod_id}/files.json"
        response = self.session.get(url)
        data = self._handle_response(response)
        return data.get("files", [])

    def get_mod_info(self, game_domain: str, mod_id: int) -> dict[str, Any]:
        """Get mod metadata from REST API."""
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/games/{game_domain}/mods/{mod_id}.json"
        response = self.session.get(url)
        return self._handle_response(response)

    def validate_key(self) -> dict[str, Any]:
        """Validate API key and return user info."""
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/users/validate.json"
        response = self.session.get(url)
        return self._handle_response(response)

    def get_tracked_mods(self) -> list[dict[str, Any]]:
        """Get all mods tracked by the current user."""
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/user/tracked_mods.json"
        response = self.session.get(url)
        return self._handle_response(response)

    def track_mod(self, game_domain: str, mod_id: int) -> None:
        """Add a mod to the user's tracked list."""
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/user/tracked_mods.json"
        response = self.session.post(url, data={"domain_name": game_domain, "mod_id": mod_id})
        self._handle_response(response)

    def untrack_mod(self, game_domain: str, mod_id: int) -> None:
        """Remove a mod from the user's tracked list."""
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/user/tracked_mods.json"
        response = self.session.delete(url, data={"domain_name": game_domain, "mod_id": mod_id})
        self._handle_response(response)
