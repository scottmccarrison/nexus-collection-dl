# nexus-collection-dl

A command-line tool to download and manage [Nexus Mods](https://www.nexusmods.com/) collections on Linux (and macOS). Works with any game - Baldur's Gate 3, Starfield, Cyberpunk 2077, you name it.

## Why?

Nexus Mods collections are a great way to grab a curated set of mods in one shot, but the official tools (Vortex, the NexusMods App) are Windows-only. If you're gaming on Linux - whether native or through Proton/Wine - there's no built-in way to download collections from the command line.

`nexus-dl` fills that gap. Point it at a collection URL, and it downloads every mod, extracts archives, generates a load order, and tracks versions so you can update later.

## Requirements

- Python 3.10+
- Nexus Mods **Premium** membership (required for API download links)
- `unrar` system package (only if the collection includes RAR archives)

## Installation

### Docker (recommended - includes LOOT support)

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
cd nexus-collection-dl
docker compose build
```

```bash
# Download a collection
docker compose run --rm nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" /mods

# Or use docker directly
docker run --rm -e NEXUS_API_KEY -v ./mods:/mods nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" /mods
```

### Setup script (auto-detects Rust for LOOT)

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
cd nexus-collection-dl
./setup.sh
source venv/bin/activate
```

If Rust is installed, the setup script automatically builds and installs `libloot` for plugin sorting. If not, the tool still works - it just won't do automatic plugin sorting for Bethesda games.

### Manual

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
cd nexus-collection-dl
python -m venv venv
source venv/bin/activate
pip install -e .
```

## Setup

1. Grab your API key from [Nexus Mods API settings](https://www.nexusmods.com/users/myaccount?tab=api%20access).
2. Export it:

```bash
export NEXUS_API_KEY="your-api-key-here"
```

## Usage

### Download a collection

```bash
nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

nexus-dl sync "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3

# Skip optional mods
nexus-dl sync --skip-optional "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

# Skip load order generation
nexus-dl sync --no-load-order "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield
```

### Check for updates

```bash
nexus-dl update ~/mods/starfield

# Preview what would change
nexus-dl update --dry-run ~/mods/starfield
```

### Regenerate load order

```bash
nexus-dl load-order ~/mods/starfield
```

Regenerates `load-order.txt` (and `plugins.txt` for Bethesda games) from the cached collection manifest without re-downloading anything.

### View status

```bash
nexus-dl status ~/mods/starfield
```

## Load order

`nexus-dl` generates load order files automatically during `sync` and `update`. The approach differs by game type:

### All games

**`load-order.txt`** - Mods listed in topological order based on:
- **Phase grouping** - The collection author's phase assignments (phase 0 loads before phase 1, etc.)
- **Collection mod rules** - Explicit before/after/requires relationships from `collection.json`
- **Author-declared dependencies** - `modRequirements` from the Nexus API (the mod author's own dependency list)

This handles BG3, Cyberpunk, and every other game. Since non-Bethesda games don't have a standardized plugin format (`.pak`, `.archive`, etc.), mod-level ordering is the best we can do.

### Bethesda games (Starfield, Skyrim, Fallout, etc.)

In addition to `load-order.txt`, Bethesda collections also get:

**`plugins.txt`** - Plugin-level load order for `.esp`/`.esm`/`.esl` files. Two sources:
1. **Collection metadata** - The collection author's intended plugin order (always available)
2. **LOOT sorting** - Automatic sorting via [libloot](https://github.com/loot/libloot) using master dependency analysis + community masterlists (when libloot is installed)

When libloot is available, LOOT-sorted order takes priority over collection metadata. Enabled/disabled status from the collection is preserved either way.

Supported Bethesda games: Starfield, Skyrim SE, Skyrim, Fallout 4, Fallout NV, Fallout 3, Oblivion, Morrowind, Enderal, plus VR variants.

### LOOT integration

LOOT is optional. Without it, `plugins.txt` uses the collection author's plugin order. With it, plugins are sorted by master dependencies using community-maintained masterlists.

To enable LOOT:
- **Docker**: Included automatically (the image builds libloot)
- **setup.sh**: Installed automatically if Rust is detected
- **Manual**: Install Rust, then `pip install maturin && maturin develop --release` in the libloot repo
- **Pre-built wheel**: Check [GitHub Releases](https://github.com/scottmccarrison/nexus-collection-dl/releases) for manylinux wheels

Masterlists are cached in `~/.cache/nexus-dl/masterlists/` and refreshed every 24 hours.

## How it works

- **sync** - Fetches the collection from the Nexus API, downloads each mod, extracts archives, parses the collection manifest, and generates load order files.
- **update** - Re-fetches the collection and downloads any mods that have newer versions. Regenerates load order.
- **load-order** - Regenerates load order from the cached manifest (no API call needed).
- **status** - Shows what's installed and whether updates are available.

State is tracked in a `.nexus-state.json` file inside your mods directory, including the cached collection manifest for offline load order regeneration.

## Supported archive formats

- ZIP
- 7z
- RAR (requires system `unrar`)
- Direct files (.pak, .esp, etc.) are moved as-is

### Installing unrar

```bash
# Ubuntu/Debian
sudo apt install unrar

# Fedora
sudo dnf install unrar

# macOS
brew install unrar
```

## License

[MIT](LICENSE)
