"""Collection URL parsing and validation."""

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class CollectionInfo:
    """Parsed collection URL information."""

    game_domain: str
    slug: str
    url: str


class CollectionParseError(Exception):
    """Raised when a collection URL cannot be parsed."""

    pass


def parse_collection_url(url: str) -> CollectionInfo:
    """
    Parse a Nexus Mods collection URL.

    Supported formats:
        - https://next.nexusmods.com/{game}/collections/{slug}
        - https://next.nexusmods.com/{game}/collections/{slug}?tab=...
        - https://www.nexusmods.com/{game}/mods/{id}?tab=collections (not supported, different format)

    Returns CollectionInfo with game_domain and slug.
    """
    parsed = urlparse(url)

    # Validate domain
    if parsed.netloc not in ("next.nexusmods.com", "www.nexusmods.com", "nexusmods.com"):
        raise CollectionParseError(
            f"Invalid domain: {parsed.netloc}. Expected next.nexusmods.com"
        )

    # Parse path: /{game}/collections/{slug}
    path_match = re.match(r"^/([^/]+)/collections/([^/?]+)", parsed.path)
    if not path_match:
        raise CollectionParseError(
            f"Invalid collection URL format: {url}\n"
            "Expected: https://next.nexusmods.com/{game}/collections/{slug}"
        )

    game_domain = path_match.group(1)
    slug = path_match.group(2)

    # Normalize URL (remove query params)
    normalized_url = f"https://next.nexusmods.com/{game_domain}/collections/{slug}"

    return CollectionInfo(
        game_domain=game_domain,
        slug=slug,
        url=normalized_url,
    )
