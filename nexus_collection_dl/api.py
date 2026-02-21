"""Nexus Mods API client for GraphQL and REST endpoints."""

import os
import time
from typing import Any

import requests

GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
REST_BASE_URL = "https://api.nexusmods.com/v1"


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
                "User-Agent": "nexus-collection-dl/0.1.0",
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

    def _handle_response(self, response: requests.Response) -> dict[str, Any]:
        """Handle API response and raise appropriate errors."""
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise NexusRateLimited(retry_after)
        if response.status_code == 403:
            data = response.json() if response.text else {}
            if "premium" in str(data).lower():
                raise NexusPremiumRequired(
                    "Premium membership required for direct download links."
                )
            raise NexusAPIError(f"Access forbidden: {data}")
        if response.status_code == 404:
            raise NexusAPIError(f"Resource not found: {response.url}")
        response.raise_for_status()
        return response.json()

    def graphql_query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a GraphQL query."""
        self._rate_limit_wait()
        response = self.session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
        )
        data = self._handle_response(response)
        if "errors" in data:
            raise NexusAPIError(f"GraphQL errors: {data['errors']}")
        return data.get("data", {})

    def get_collection_mods(self, game_domain: str, slug: str) -> dict[str, Any]:
        """
        Get collection metadata and mod list from the latest revision.

        Returns dict with:
            - name: collection name
            - revision: revision number
            - mods: list of mod info dicts
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
        for mf in mod_files:
            file_info = mf.get("file", {})
            mod_info = file_info.get("mod", {})
            if not file_info or not mod_info:
                continue

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
                    "file_id": file_info.get("fileId"),
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

    def get_download_url(self, game_domain: str, mod_id: int, file_id: int) -> str:
        """
        Get direct download URL for a mod file (requires Premium).

        Returns the download URL string.
        """
        self._rate_limit_wait()
        url = f"{REST_BASE_URL}/games/{game_domain}/mods/{mod_id}/files/{file_id}/download_link.json"
        response = self.session.get(url)
        data = self._handle_response(response)

        # Response is a list of CDN options, pick the first one
        if not data or not isinstance(data, list):
            raise NexusAPIError(f"Unexpected download link response: {data}")

        return data[0].get("URI")

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
