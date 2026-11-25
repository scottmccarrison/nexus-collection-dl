# nexus-collection-dl

Download and manage [Nexus Mods](https://www.nexusmods.com/) collections on Linux and macOS. Works with any game - Baldur's Gate 3, Starfield, Cyberpunk 2077, you name it.

## Quick start

```bash
# Install
uv tool install "nexus-collection-dl @ git+https://github.com/scottmccarrison/nexus-collection-dl@main"

# Configure (detects Steam, asks where collections live)
nexus-dl setup

# Download a collection
nexus-dl sync https://next.nexusmods.com/skyrimspecialedition/collections/XXXX ~/nexus-collections

# Install mods into your game
nexus-dl deploy ~/nexus-collections/CollectionName
```

Or just run `nexus-dl` after setup to open the interactive shell.

**New to the command line?** Read the [Beginner's Guide](GUIDE.md) - it walks through every step including installing a terminal.

## Common tasks

| I want to... | Command |
|---|---|
| Download a collection | `nexus-dl sync <url> <dir>` |
| Install mods into my game | `nexus-dl deploy <dir>` |
| See what would be installed | `nexus-dl deploy <dir> --dry-run` |
| Remove deployed mods | `nexus-dl undeploy <dir>` |
| Add a single mod | `nexus-dl add <nexus-url> <dir>` |
| Turn off a mod temporarily | `nexus-dl disable --mod-id 12345 <dir>` |
| Remove a mod completely | `nexus-dl remove --mod-id 12345 <dir>` |
| Reclaim disk space | `nexus-dl gc <dir> --apply` |
| See what I have | `nexus-dl mods <dir>` |
| Check for a newer version | `nexus-dl update` or `/update` in the shell |
| Make "Mod manager download" clicks work | `nexus-dl handler install` |

Every command takes `--help` for its options. Add `--json` before any command for machine-readable output.

## Installation

Python 3.10+ required.

**With `uv tool` (recommended):**

```bash
uv tool install "nexus-collection-dl @ git+https://github.com/scottmccarrison/nexus-collection-dl@main"
```

**With `pip`:**

```bash
pip install "nexus-collection-dl @ git+https://github.com/scottmccarrison/nexus-collection-dl@main"
```

**With Docker (includes LOOT plugin sorting):**

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
cd nexus-collection-dl
docker compose build
```

If Rust is installed, the setup script builds `libloot` for plugin sorting. Without it, the tool still works - it just won't auto-sort plugins for Bethesda games.

### Install from beta

Beta gets new features first and can change between points:

```bash
uv tool install "nexus-collection-dl @ git+https://github.com/scottmccarrison/nexus-collection-dl@beta"
```

## Setup

```bash
nexus-dl setup
```

Detects your Steam root, asks where collections live, and offers to install the `nxm://` click handler so "Mod manager download" clicks land in nexus-dl. For scripts: `nexus-dl setup --non-interactive --collections-root ~/nexus-collections`.

## Shell

Run `nexus-dl` with no arguments to open the interactive shell over your collections. Type `/help` for commands. Key ones:

- `/sync` - download or refresh a collection
- `/deploy` - install mods into your game
- `/mods` - see what you have
- `/status` - health check and overview
- `/config` - change where collections live
- `/update` - check for a newer version

The shell also notices when you click "Mod manager download" on the Nexus site and offers to handle the download.

## Machine-readable output

Add `--json` before any command for structured output on stdout:

```bash
nexus-dl --json status ~/nexus-collections/MyCollection
```

Prose goes to stderr, so `2>/dev/null` gives you clean JSON.

## Free vs Premium accounts

Premium users download directly through the API. Free users register a pending download and finish it manually from the Nexus website, then run `nexus-dl import` to bring the files in. The tool tells you which mode it is using.

## Load order

The tool generates `Plugins.txt` from the collection's manifest order. For Bethesda games (Skyrim, Fallout, Starfield), it also writes the `*Custom.ini` needed for mods to load. With `libloot` installed, plugins are LOOT-sorted instead of using manifest order.

## Upgrading and downgrading

The tool checks GitHub for newer versions on every command. In the shell, `/update` shows the current version and how to upgrade.

**Upgrade:**

```bash
uv tool install --from git+https://github.com/scottmccarrison/nexus-collection-dl@main nexus-collection-dl --force
```

**Downgrade** (replace `0.4.0` with the version you want):

```bash
uv tool install --from git+https://github.com/scottmccarrison/nexus-collection-dl@v0.4.0 nexus-collection-dl --force
```

Config, handler mappings, and collection state survive both. Features from a newer version are unavailable in an older one.

## Command reference

| Command | What it does |
|---|---|
| `setup` | Configure collections root and game detection |
| `sync <url> <dir>` | Download a collection |
| `deploy <dir>` | Install mods into your game directory |
| `undeploy <dir>` | Remove deployed mod files |
| `add <url> <dir>` | Add a single mod |
| `add-local <name> <dir>` | Register a local mod |
| `import <dir>` | Import manually downloaded files |
| `remove --mod-id N <dir>` | Remove a mod from state and disk |
| `enable / disable --mod-id N <dir>` | Turn a mod on/off |
| `mods <dir>` | List tracked mods |
| `collections [root]` | List collections |
| `games [root]` | List games with mod counts |
| `status <dir>` | Show status vs latest revision |
| `gc <dir>` | Reclaim disk from old archives |
| `load-order <dir>` | Regenerate load order |
| `handler` | Manage nxm:// click handling |
| `update <dir>` | Check for updated mods |
| `track-sync` | Manage Nexus tracked-mod sync |
| `health` | Run installation health checks |
| `shell` | Open the interactive shell |
| `nxm <url>` | Handle an nxm:// link (called by the browser) |

Every command takes `--help`. `--json` (before the subcommand) gives machine-readable output.

## More

- **[Beginner's Guide](GUIDE.md)** - full walkthrough for new users
- **[Discussions](https://github.com/scottmccarrison/nexus-collection-dl/discussions)** - questions and beta testing
- **[Issues](https://github.com/scottmccarrison/nexus-collection-dl/issues)** - bug reports
- **[Agent skill](.agents/skills/nexus-dl/)** - for AI harnesses

## License

MIT
