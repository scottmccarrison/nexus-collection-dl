# nexus-collection-dl

A tool to download and manage [Nexus Mods](https://www.nexusmods.com/) collections on Linux (and macOS). Works with any game - Baldur's Gate 3, Starfield, Cyberpunk 2077, you name it. Comes with a local web UI and a CLI.

## Why?

Nexus Mods collections are a great way to grab a curated set of mods in one shot, but the official tools (Vortex, the NexusMods App) are Windows-only. If you're gaming on Linux - whether native or through Proton/Wine - there's no built-in way to download collections from the command line.

`nexus-dl` fills that gap. Point it at a collection URL, and it downloads every mod, extracts archives, generates a load order, and tracks versions so you can update later.

**New to the command line?** Check out the [Beginner's Guide](GUIDE.md) for a full walkthrough.

## Requirements

- Python 3.10+
- Nexus Mods account (free or Premium - see [Free vs Premium](#free-vs-premium-accounts) below)
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

Planning to use "Mod manager download" click-through? Also store the key with `nexus-dl handler set-api-key` (hidden prompt): browser-launched downloads cannot see your shell's environment variables. See the [nxm:// link handler](#nxm-link-handler) section.

The tool checks GitHub for newer versions on every command and prints an upgrade notice if one is available.

## Web UI

Launch the local web interface for day-to-day mod management:

```bash
nexus-dl serve ~/mods/starfield

# Custom port
nexus-dl serve ~/mods/starfield --port 8080
```

Opens a dark-themed dashboard at `http://127.0.0.1:5000` with:

- **Dashboard** - collection info, deployment status, quick action buttons (sync, update, deploy, undeploy)
- **Mods** - full mod table with status badges, add mod by URL, register local mods
- **Background tasks** - long-running operations (sync, update, deploy) stream progress in real time

There's also a standalone entry point:

```bash
nexus-dl-web ~/mods/starfield
```

## CLI Usage

All operations are also available from the command line.

### Download a collection

```bash
nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

nexus-dl sync "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3

# Skip optional mods
nexus-dl sync --skip-optional "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

# Skip load order generation
nexus-dl sync --no-load-order "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

# Keep archives intact (for external mod managers like Stardrop)
nexus-dl sync --no-extract "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield
```

Each collection gets its own named subfolder inside the mods directory (e.g., `~/mods/starfield/My Collection Name/`), so multiple collections for the same game stay organized.

Re-running `sync` is resumable - already-downloaded mods are skipped, so you can safely abort and pick up where you left off.

After sync and deploy, the tool reports **file conflicts** - cases where multiple mods wrote to the same file path. This helps you spot overlapping mods and decide on load order priority.

### Check for updates

```bash
nexus-dl update ~/mods/starfield

# Preview what would change
nexus-dl update --dry-run ~/mods/starfield

# Keep archives intact during update
nexus-dl update --no-extract ~/mods/starfield
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

**How deployment stays safe:** any file that already exists at a destination (regular file or symlink) is backed up byte-for-byte, including permissions and symlink targets, in a private `.nexus-backups/` directory inside the collection folder before it is replaced. `undeploy` restores those originals. Every deployed file is fingerprinted (SHA256 for copies, exact link target for symlinks), and if a deployed file was changed outside nexus-dl, removal or replacement is refused and the path is reported instead of destroying your edit. Each step is journaled, so an interrupted deploy or undeploy resumes cleanly on the next run. Limitations: deployments made by versions before 0.2.1 have no backups, so originals they replaced cannot be restored, and destination paths reached through symlinked parent directories are refused (fail closed) rather than followed.

**SFSE/script extender note:** If the collection includes SFSE, the tool deploys `sfse_loader.exe` to the game root and prints launch instructions. For non-Steam games, set your Steam shortcut TARGET to `sfse_loader.exe`. Do not rename it - SFSE needs the original game executable alongside it.

### Add individual mods

```bash
# Add a mod by URL (downloads the main file automatically)
nexus-dl add "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield

# Pick a specific file from the mod
nexus-dl add "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield --file-id 456

# Skip load order regeneration
nexus-dl add --no-load-order "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield

# Keep the archive intact
nexus-dl add --no-extract "https://www.nexusmods.com/starfield/mods/123" ~/mods/starfield
```

Downloads a single mod and registers it as a "manual" mod. Manual mods are protected from removal during `update` - they won't be flagged as "removed from collection" since they were never part of it. Manual mods load after all collection mods (phase 999).

If the tool can't auto-select a file (no main file category), it lists all available files so you can re-run with `--file-id`.

### nxm:// link handler

Handle "Mod manager download" links from the Nexus website directly, without another mod manager. No daemon or background service is involved: when you click the button (and accept the browser's first-run "Launch application" confirmation for nxm:// links), your browser hands the `nxm://` link to your desktop environment, the desktop environment looks up the registered handler, and the OS launches nexus-dl as a short-lived process with the link as its argument. The click runs headless (no terminal window); the outcome arrives as a desktop notification, and every run appends one redacted status line to `~/.local/state/nexus-dl/nxm.log` as the diagnostic fallback. When the download finishes, the process exits.

One catch: a browser-launched process sees your desktop session's environment, not your shell's, so a `NEXUS_API_KEY` exported in `.bashrc` never reaches it. Store the key once instead. The prompt is hidden: the key is never echoed, never lands in shell history, and never appears in the process list:

```bash
nexus-dl handler set-api-key
```

Every command resolves the key in this order: `--api-key` flag, then `NEXUS_API_KEY`, then the stored key. `nexus-dl handler status` shows which source is active (never the value), and `nexus-dl handler remove-api-key` deletes the stored key.

**Full setup, start to finish** (from a source checkout; this is also the smoke test):

```bash
# 1. Install dependencies (use --extra dev if you want to run the tests)
uv sync
uv sync --extra dev

# 2. Create a disposable collection with sync. sync creates the
#    per-collection subfolder holding .nexus-state.json that the
#    handler downloads into.
nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" "$HOME/mods/nxm-smoke"

# 3. Store your API key for browser-launched clicks (hidden prompt)
nexus-dl handler set-api-key

# 4. Map the game to the SYNCED COLLECTION directory: the subfolder
#    created by sync that contains .nexus-state.json, not its parent.
#    (Use $HOME, not a quoted ~: the shell only expands ~ when unquoted)
nexus-dl handler map starfield "$HOME/mods/nxm-smoke/My Collection"

# 5. Register nexus-dl as the desktop nxm:// handler (user-level, opt-in)
nexus-dl handler install
```

Now click **Mod manager download** once on any mod file page for that game:

- The first click downloads that one file into the mapped collection, verifies it, extracts it, and reports the outcome as a desktop notification (the registered handler runs headless and requests notifications explicitly). Nothing is deployed to the game; run `nexus-dl deploy` when you're ready.
- Clicking again for the same file re-downloads and re-verifies it: the default flow deletes the archive after extracting, so there is nothing left to dedupe against. A re-click with a retained verified archive triggers no download: an entry already marked downloaded (for example with `--no-extract`) reports "already downloaded" with zero network calls, and an entry whose extraction previously failed finishes extracting from the retained archive (no file download; with track-sync enabled a background tracked-mod sync may still make API calls). Every click also appends a redacted line to `~/.local/state/nexus-dl/nxm.log`.
- `nexus-dl handler uninstall` removes the desktop entry and restores the app that handled nxm:// links before, when one was recorded and nexus-dl is still the default.

Useful flags when running `nxm` yourself:

```bash
# Choose the collection explicitly (overrides the game mapping)
nexus-dl nxm "nxm://..." --mods-dir "$HOME/mods/starfield/My Collection"

# Save the verified archive to a folder without touching any collection
nexus-dl nxm "nxm://..." --output ~/Downloads

# Keep the archive intact / skip load order regeneration
nexus-dl nxm "nxm://..." --no-extract --no-load-order
```

Advanced footnote: run a copied link manually without putting its credentials in shell history. Run `read` first, THEN paste the nxm:// link on its own line and press Enter. (Pasting the link onto the `read` command line just hangs, because `read` waits for a newline before it returns.)

```bash
read -s -r NXM_LINK
# paste the nxm:// link here, then press Enter
nexus-dl nxm "$NXM_LINK" --output "$HOME/Downloads"
```

Notes:

- `handler map` requires an already-synced collection: the directory must contain `.nexus-state.json` (the collection subfolder created by `nexus-dl sync`, not its parent), and its game must match the mapping key.
- The stored API key lives in `~/.config/nexus-dl/api-key` with owner-only (0600) permissions. No command ever prints its value.
- The link's `key`/`expires` credentials are used for the download request only and are never stored in state, logs, or error messages. The `user_id` is a local check that the link matches your API key's account; it is never sent to the API.
- Free accounts need a fresh signed link from the website. If a link has expired or carries no credentials, nexus-dl says so and tells you to click "Mod manager download" again - nothing is downloaded or written.
- Pasting an `https://` mod-page URL into `nexus-dl nxm` is refused with an explanation: the nxm:// link comes from the "Mod manager download" button (right-click it and copy the link address).
- Downloads are verified before they are published (exact size, manifest MD5 when known); a file with neither a positive expected size nor a well-formed cached checksum is refused with nothing written. A re-click with the verified archive still on disk triggers no download traffic: an entry already marked downloaded reports "already downloaded" with zero network calls, and an entry whose extraction failed finishes extracting from the retained archive, verified against the cached manifest hash when one is known (with track-sync enabled, a background tracked-mod sync may still make API calls); the default flow deletes the archive after a successful extraction, so a re-click then re-downloads and re-verifies it.
- Extraction publishes staged files one by one, not atomically as a set: a rare mid-publication failure (disk full, permissions) can leave a subset of extracted files in place while the error is reported and staging is cleaned up; re-running (the link or `nexus-dl import`) is safe and overwrites the partial set.
- Re-running `handler install` is idempotent: the desktop entry is always rewritten (refreshing entries from older versions). Install also probes what the click flow needs (xdg-mime, notify-send, and the desktop's notification switch where one is known), warns about anything missing or switched off, and runs a best-effort notification-daemon self-test pointing at `~/.local/state/nexus-dl/nxm.log`. Warnings never block the install.
- Platform support and the notification gotchas have their own subsections below. The one universal limit: Flatpak/Snap browsers generally cannot launch host desktop handlers, which is unsupported - use a native browser build or run `nexus-dl nxm` on a copied link.
- `nxm://` collection links are not supported - use `nexus-dl sync` with the collection URL; the handler detects them and says so.
- Nothing is deployed automatically. Run `nexus-dl deploy` when you're ready.

#### Platform support

The click-through flow was live-tested end to end on one machine: Linux Mint (Cinnamon) with Firefox. Everything else below is expected to work through standard mechanisms and honestly marks itself as untested. nexus-dl never gates install on desktop identity: `handler install` probes the tools and settings that actually matter (xdg-mime, notify-send, and the notification switch where one is known) and warns about what it finds.

| Desktop | Status | Notes |
|---|---|---|
| Cinnamon (Linux Mint) | Tested live | With Firefox. Mint ships with Settings > Notifications > "Display notifications" off; the install probe warns when it finds the switch off (see [Notifications](#notifications)). |
| GNOME | Expected to work, untested | Standard xdg-mime/desktop-entry handling. Its `show-banners` switch must be on for click notifications; the install probe warns when it finds it off. |
| MATE, XFCE, Budgie | Expected to work, untested | GIO-family desktops; standard xdg-mime/desktop-entry handling. No notification switch to probe. |
| KDE Plasma (including CachyOS) | Expected to work, untested | KIO-based rather than GIO, but scheme handlers register through the same xdg-mime/desktop-entry mechanisms and notify-send works. No notification switch to probe. |
| Tiling setups (i3, Sway, Hyprland) | Expected to work, untested | Needs a notification daemon (`dunst`, `mako`, ...) for click feedback. |
| Flatpak/Snap browsers | Unsupported | Sandboxed browsers generally cannot launch host desktop handlers. Use a native browser build, or copy the link and run `nexus-dl nxm` yourself. |

#### Notifications

The desktop notification is the normal confirmation for a click: successes and failures both report one, and every run also appends one redacted status line to `~/.local/state/nexus-dl/nxm.log` whatever the desktop does. Two things silently take the notification away while the download still works:

- **The desktop's notification switch is off.** Linux Mint/Cinnamon ships with Settings > Notifications > "Display notifications" off, so a fresh Mint install shows nothing after a click even though the download worked. This was a real support trap; GNOME hides the same switch behind "Show banners". `nexus-dl handler install` probes the switch where it is known and warns when it finds it off. Fixes:
  - Cinnamon: `gsettings set org.cinnamon.desktop.notifications display-notifications true`
  - GNOME: `gsettings set org.gnome.desktop.notifications show-banners true`
- **No notification tooling.** nexus-dl asks for notifications through `notify-send` (package `libnotify`), and tiling setups need a notification daemon (`dunst`, `mako`, ...). Without them clicks complete silently; the install probe warns about both.

When in doubt, the log is the reliable channel: `tail -n 5 ~/.local/state/nexus-dl/nxm.log` after a click.

#### Common problems

For a click that seems to do nothing, start with the [troubleshooting section](GUIDE.md#troubleshooting): short version, check the log first, because a new line means the click ran and only the feedback is missing. A second click re-downloads and re-verifies a file the default flow already extracted; that is by design (the archive is deleted after extraction), and re-clicks are free only when the verified archive was kept. Peer support and a fuller problem list live in [Discussion #61: Start here: common problems and fixes](https://github.com/scottmccarrison/nexus-collection-dl/discussions/61).

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

When enabled, the "tracked" status on nexusmods.com mirrors your local mod list. After `sync`, `update`, `add`, or `add-local`, tracked mods are automatically updated. This is most useful when viewing an individual mod page before downloading - if it shows "tracking", you know it's already in your local loadout. Note that in the mod browse/list view, the "downloaded" badge takes priority over "tracking" in the Nexus UI, so tracked status is only visible on individual mod pages.

Opt-in only - won't touch your tracked mods unless you explicitly enable it. Disabling preserves whatever's currently tracked on Nexus. Only affects mods for the collection's game domain (your tracked Skyrim mods are safe when syncing a Starfield collection). Local-only mods (from `add-local`) are skipped since they don't have Nexus mod IDs.

### Remove deployed mods

```bash
nexus-dl undeploy ~/mods/starfield
```

Removes all symlinks/copies that were deployed and restores the game directory to its pre-mod state, including original files and config files (`plugins.txt`, the game's custom INI) that existed before deploying. Removal is verified: if a deployed file was modified outside nexus-dl, or its backup is missing, that path is refused and reported, the entry stays in state, and re-running `undeploy` after resolving it finishes the job.

### Import manually downloaded files

```bash
# Point import at the synced collection folder - the subfolder created
# by 'nexus-dl sync' that contains .nexus-state.json, not its parent
nexus-dl import "$HOME/mods/starfield/My Collection"

# Keep archives intact
nexus-dl import --no-extract "$HOME/mods/starfield/My Collection"
```

Matches files you downloaded through the browser to pending mods, extracts archives, and regenerates load order. See [Free vs Premium](#free-vs-premium-accounts) below for the full workflow.

### View status

```bash
nexus-dl status ~/mods/starfield
```

## Free vs Premium accounts

Both free and Premium Nexus Mods accounts work with `nexus-dl`. The difference is how mod files get downloaded.

**Premium** accounts can download files directly through the API, so `sync`, `update`, and `add` handle everything automatically.

**Free** accounts can't use the Nexus download API (this is a Nexus Mods restriction, not ours), but every other API endpoint works fine - collection metadata, mod info, file listings, tracked mods. The tool uses these free endpoints to do everything except the actual file download. Once files are local, the experience is identical: extraction, load order, deployment, status tracking, and updates all work the same way.

### Free account workflow

```bash
# 1. Sync the collection - fetches metadata, caches manifest, prints download
#    links. sync creates a per-collection subfolder (e.g. "My Collection")
#    and prints its path; steps 3-5 use that subfolder, not the parent.
nexus-dl sync "https://next.nexusmods.com/starfield/collections/xyz789" ~/mods/starfield

# 2. You'll see a table like:
#    Mod               | Filename              | Size    | URL
#    Starfield SE      | StarfieldSE-1.2.zip   | 45.2 MB | https://www.nexusmods.com/...
#    Better Textures   | BetterTex-v3.7z       | 120 MB  | https://www.nexusmods.com/...
#    ...

# 3. Click each URL, download through Nexus (free countdown timer), and save
#    the files into the collection subfolder: $HOME/mods/starfield/My Collection

# 4. Import the downloaded files
nexus-dl import "$HOME/mods/starfield/My Collection"

# 5. Deploy as usual
nexus-dl deploy "$HOME/mods/starfield/My Collection"
```

The `import` command matches downloaded files by filename (case-insensitive), extracts archives, updates the state, and regenerates load order. If some files are missing, it reports which mods are still pending.

Re-running `sync` or `update` as a free user won't regress mods you've already imported - only new or changed mods show as pending.

The web UI also supports this workflow: the dashboard shows a "Pending Downloads" card with clickable links, and an "Import Downloads" button.

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

- **serve** - Launches a local web UI for managing mods from your browser.
- **sync** - Fetches the collection from the Nexus API and caches the manifest. Premium: downloads each mod (skipping any already downloaded), extracts archives into per-collection subdirectories, generates load order, and reports file conflicts. Free: prints browser download links for manual download.
- **update** - Re-fetches the collection and downloads any mods that have newer versions. Regenerates load order.
- **add** - Downloads a single mod by URL and registers it as a manual mod (phase 999, protected from update removal).
- **add-local** - Registers an already-present mod in the state without downloading anything.
- **import** - Matches manually downloaded files to pending mods, extracts archives, and regenerates load order. Used with free accounts after downloading files through the browser.
- **deploy** - Classifies files by type and symlinks (or copies) them to the correct game directory locations. Handles SFSE, Data/ assets, plugins, and Proton config files. Backs up any pre-existing destination file first and journals every mutation for crash recovery.
- **undeploy** - Removes all deployed files using the tracked manifest, restoring backed-up originals. Refuses (and reports) entries that were changed externally or whose backup is missing, keeping them retryable.
- **track-sync** - Manages Nexus tracked-mod sync (enable/disable/push). When enabled, the tracked bell icon on the website matches your local loadout.
- **load-order** - Regenerates load order from the cached manifest (no API call needed).
- **status** - Shows what's installed and whether updates are available.

Both the web UI and CLI share the same service layer, so all operations behave identically regardless of interface.

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

## Testing beta builds

The `beta` branch carries new work that is usable but lightly tested. It is always ahead of `main`, and behavior can change between beta points. Testing so far is single-machine (Linux Mint/Cinnamon with Firefox); the current beta (v0.3.0-beta.1) adds the native `nxm://` click-through handler described above.

Install or switch to the beta channel:

```bash
uv tool install "nexus-collection-dl @ git+https://github.com/scottmccarrison/nexus-collection-dl@beta"
```

Report what you find in [Discussion #62: Beta testing program: v0.3.0-beta.1](https://github.com/scottmccarrison/nexus-collection-dl/discussions/62) (pinned). Include the redacted lines from `~/.local/state/nexus-dl/nxm.log`; they contain no credentials.

## License

[MIT](LICENSE)
