# nexus-collection-dl

A command-line tool to download and manage [Nexus Mods](https://www.nexusmods.com/) collections on Linux (and macOS). Works with any game — Baldur's Gate 3, Starfield, Cyberpunk 2077, you name it.

## Why?

Nexus Mods collections are a great way to grab a curated set of mods in one shot, but the official tools (Vortex, the NexusMods App) are Windows-only. If you're gaming on Linux — whether native or through Proton/Wine — there's no built-in way to download collections from the command line.

`nexus-dl` fills that gap. Point it at a collection URL, and it downloads every mod, extracts archives, and tracks versions so you can update later.

## Requirements

- Python 3.10+
- Nexus Mods **Premium** membership (required for API download links)
- `unrar` system package (only if the collection includes RAR archives)

## Installation

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
cd nexus-collection-dl
pip install -e .
```

Or with a virtual environment:

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
# Baldur's Gate 3
nexus-dl sync "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3

# Starfield
nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

# Skip optional mods
nexus-dl sync --skip-optional "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3
```

### Check for updates

```bash
nexus-dl update ~/mods/bg3

# Preview what would change
nexus-dl update --dry-run ~/mods/bg3
```

### View status

```bash
nexus-dl status ~/mods/bg3
```

## How it works

- **sync** — Fetches the collection manifest from the Nexus API, downloads each mod archive, extracts it, and records the installed version.
- **update** — Re-fetches the manifest and downloads any mods that have newer versions.
- **status** — Shows what's installed and whether updates are available.

State is tracked in a `.nexus-state.json` file inside your mods directory.

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
