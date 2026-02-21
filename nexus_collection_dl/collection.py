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


@dataclass
class ModInfo:
    """Parsed mod page URL information."""

    game_domain: str
    mod_id: int
    url: str


class CollectionParseError(Exception):
    """Raised when a collection URL cannot be parsed."""

    pass


class ModParseError(Exception):
    """Raised when a mod URL cannot be parsed."""

    pass


def parse_collection_url(url: str) -> CollectionInfo:
    """
    Parse a Nexus Mods collection URL.

    Supported formats:
        - https://next.nexusmods.com/{game}/collections/{slug}
        - https://www.nexusmods.com/games/{game}/collections/{slug}
        - Either format with ?tab=... query params

    Returns CollectionInfo with game_domain and slug.
    """
    parsed = urlparse(url)

    # Validate domain
    if parsed.netloc not in ("next.nexusmods.com", "www.nexusmods.com", "nexusmods.com"):
        raise CollectionParseError(
            f"Invalid domain: {parsed.netloc}. Expected next.nexusmods.com"
        )

    # Parse path: /{game}/collections/{slug} or /games/{game}/collections/{slug}
    path_match = re.match(r"^/(?:games/)?([^/]+)/collections/([^/?]+)", parsed.path)
    if not path_match:
        raise CollectionParseError(
            f"Invalid collection URL format: {url}\n"
            "Expected: https://next.nexusmods.com/{game}/collections/{slug}\n"
            "      or: https://www.nexusmods.com/games/{game}/collections/{slug}"
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


def parse_mod_url(url: str) -> ModInfo:
    """
    Parse a Nexus Mods mod page URL.

    Supported formats:
        - https://www.nexusmods.com/{game}/mods/{id}
        - https://next.nexusmods.com/{game}/mods/{id}
        - Either format with ?tab=... query params

    Returns ModInfo with game_domain and mod_id.
    """
    parsed = urlparse(url)

    if parsed.netloc not in ("next.nexusmods.com", "www.nexusmods.com", "nexusmods.com"):
        raise ModParseError(
            f"Invalid domain: {parsed.netloc}. Expected nexusmods.com"
        )

    path_match = re.match(r"^/([^/]+)/mods/(\d+)", parsed.path)
    if not path_match:
        raise ModParseError(
            f"Invalid mod URL format: {url}\n"
            "Expected: https://www.nexusmods.com/{{game}}/mods/{{id}}"
        )

    game_domain = path_match.group(1)
    mod_id = int(path_match.group(2))
    normalized_url = f"https://www.nexusmods.com/{game_domain}/mods/{mod_id}"

    return ModInfo(
        game_domain=game_domain,
        mod_id=mod_id,
        url=normalized_url,
    )
