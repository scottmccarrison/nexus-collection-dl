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

### Deploy to game directory

```bash
# Auto-detect game path from Steam
nexus-dl deploy ~/mods/starfield

# First time with a non-Steam game: specify paths manually
nexus-dl deploy ~/mods/starfield \
  --game-dir /mnt/games/SteamLibrary/steamapps/common/Starfield \
  --prefix ~/.steam/debian-installation/steamapps/compatdata/1234567/pfx

# Copy files instead of symlinking
nexus-dl deploy ~/mods/starfield --copy

# Preview what would be deployed
nexus-dl deploy ~/mods/starfield --dry-run
```

Creates symlinks (or copies) from your staging directory into the game's install directory. For Bethesda games on Proton, it also writes `plugins.txt` and `StarfieldCustom.ini` (or equivalent) to the correct Wine prefix paths.

`--game-dir` and `--prefix` are saved after the first deploy, so subsequent runs only need `nexus-dl deploy ~/mods/starfield`.

Redeploying is safe - it removes previously deployed files before creating new ones, so no orphaned symlinks accumulate.

**SFSE/script extender note:** If the collection includes SFSE, the tool deploys `sfse_loader.exe` to the game root and prints launch instructions. For non-Steam games, set your Steam shortcut TARGET to `sfse_loader.exe`. Do not rename it - SFSE needs the original game executable alongside it.

### Add individual mods

```bash
# Add a mod by URL (downloads the main file automatically)
nexus-dl add "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield

# Pick a specific file from the mod
nexus-dl add "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield --file-id 456

# Skip load order regeneration
nexus-dl add --no-load-order "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield
```

Downloads a single mod and registers it as a "manual" mod. Manual mods are protected from removal during `update` - they won't be flagged as "removed from collection" since they were never part of it. Manual mods load after all collection mods (phase 999).

If the tool can't auto-select a file (no main file category), it lists all available files so you can re-run with `--file-id`.

### Register local mods

```bash
nexus-dl add-local "My Custom Mod" ~/mods/starfield
```

Registers an already-present mod that you placed in the mods directory manually. Useful for custom patches, merged plugins, or mods from other sources. Like `add`, these are tracked as manual mods and won't be removed by `update`.

### Tracked-mod sync

```bash
# Enable - syncs tracked mods on Nexus to match your local loadout
nexus-dl track-sync enable ~/mods/starfield

# Disable - stops future syncs, leaves existing tracked mods alone
nexus-dl track-sync disable ~/mods/starfield

# One-shot manual push (works regardless of enable/disable)
nexus-dl track-sync push ~/mods/starfield
```

When enabled, the "tracked" bell icon on nexusmods.com mirrors your local mod list. After `sync`, `update`, `add`, or `add-local`, tracked mods are automatically updated so browsing the site shows which mods you already have installed.

Opt-in only - won't touch your tracked mods unless you explicitly enable it. Disabling preserves whatever's currently tracked on Nexus. Only affects mods for the collection's game domain (your tracked Skyrim mods are safe when syncing a Starfield collection). Local-only mods (from `add-local`) are skipped since they don't have Nexus mod IDs.

### Remove deployed mods

```bash
nexus-dl undeploy ~/mods/starfield
```

Removes all symlinks/copies that were deployed and restores the game directory to its pre-mod state.

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
- **add** - Downloads a single mod by URL and registers it as a manual mod (phase 999, protected from update removal).
- **add-local** - Registers an already-present mod in the state without downloading anything.
- **deploy** - Classifies files by type and symlinks (or copies) them to the correct game directory locations. Handles SFSE, Data/ assets, plugins, and Proton config files.
- **undeploy** - Removes all deployed files using the tracked manifest, restoring the game directory.
- **track-sync** - Manages Nexus tracked-mod sync (enable/disable/push). When enabled, the tracked bell icon on the website matches your local loadout.
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
