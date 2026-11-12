# Beginner's Guide

A step-by-step walkthrough for downloading and managing Nexus Mods collections on Linux. No prior command-line experience required - every command is copy-pasteable with an explanation of what it does.

## What this tool does

`nexus-dl` lets you download entire mod collections from [Nexus Mods](https://www.nexusmods.com/) on Linux. Collections are curated mod packs that other players put together - think of them as "mod playlists" where someone has already figured out which mods work well together and in what order they should load.

Normally, downloading collections requires Vortex or the Nexus Mods app, which only run on Windows. This tool does the same thing from a Linux terminal (or from a local web page in your browser if you prefer clicking over typing).

It works with any game on Nexus Mods - Baldur's Gate 3, Starfield, Cyberpunk 2077, Skyrim, Stardew Valley, and everything else.

## What you need before starting

1. **A Nexus Mods account** - Both free and Premium accounts work. Premium lets the tool download files automatically. Free accounts work too - you just download the files yourself through the browser and the tool handles everything else (metadata, extraction, load order). More on this below.

2. **Python 3.10 or newer** - This comes pre-installed on most Linux distributions. We'll check in a moment.

3. **A Nexus Mods API key** - This is a secret code that lets the tool talk to Nexus Mods on your behalf. We'll get this together below.

4. **git** - Used to download the tool itself. Also pre-installed on most systems.

## Opening a terminal

A terminal is a text window where you type commands. Here's how to open one:

- **Ubuntu / Pop!_OS / Linux Mint**: Press `Ctrl + Alt + T`
- **Fedora / GNOME**: Press `Super` (the Windows key), type "Terminal", and click it
- **KDE (Kubuntu, Fedora KDE)**: Press `Ctrl + Alt + T`, or find "Konsole" in your app menu
- **Steam Deck (Desktop Mode)**: Tap the Steam icon in the taskbar, go to System > Konsole. Or find "Konsole" in the app launcher.

You should see a window with a blinking cursor. That's where you'll type the commands from this guide.

## Checking Python

Copy and paste this into your terminal, then press Enter:

```bash
python3 --version
```

You should see something like `Python 3.12.3`. Any version 3.10 or higher works.

If you get "command not found":

```bash
# Ubuntu/Debian/Pop!_OS
sudo apt install python3 python3-venv python3-pip

# Fedora
sudo dnf install python3

# Steam Deck - Python is pre-installed, but if missing:
sudo pacman -S python
```

## Installing nexus-dl

These commands download the tool and set it up. Run them one at a time:

```bash
git clone https://github.com/scottmccarrison/nexus-collection-dl.git
```

This downloads the tool's code into a folder called `nexus-collection-dl`.

```bash
cd nexus-collection-dl
```

This moves you into that folder.

```bash
./setup.sh
```

This installs everything the tool needs. It creates an isolated environment (called a "virtual environment") so it won't interfere with anything else on your system. It takes a minute or two.

```bash
source venv/bin/activate
```

This activates the tool's environment. You'll notice your terminal prompt changes - it'll show `(venv)` at the beginning. **You need to run this command every time you open a new terminal** before using `nexus-dl`. (We'll cover how to make this automatic later.)

Verify it worked:

```bash
nexus-dl --help
```

You should see a list of commands like `sync`, `update`, `deploy`, etc. The tool also checks for newer versions automatically - if an update is available, you'll see a notice when you run any command.

## Getting your API key

An API key is like a password that lets the tool talk to Nexus Mods on your behalf.

1. Go to [nexusmods.com](https://www.nexusmods.com/) and log in
2. Click your profile picture in the top right
3. Click **Site preferences**
4. Click the **API Keys** tab
5. Under "Personal API Key", type a name for the key (anything works - "nexus-dl" is fine)
6. Click **Request an API key**
7. Copy the long string of letters and numbers that appears

**Keep this key private.** Treat it like a password - don't share it or post it publicly.

## Setting your API key

The tool looks for your API key in an "environment variable" called `NEXUS_API_KEY`. An environment variable is just a named value that programs can read.

### Quick method (lasts until you close the terminal)

```bash
export NEXUS_API_KEY="paste-your-key-here"
```

Replace `paste-your-key-here` with the key you copied. Keep the quotes.

### Permanent method (recommended)

To avoid setting the key every time you open a terminal, add it to your shell's startup file:

```bash
echo 'export NEXUS_API_KEY="paste-your-key-here"' >> ~/.bashrc
```

Replace `paste-your-key-here` with your actual key. Then reload the file:

```bash
source ~/.bashrc
```

Now the key will be set automatically every time you open a terminal.

**Note:** If your terminal uses `zsh` instead of `bash` (you can check with `echo $SHELL`), use `~/.zshrc` instead of `~/.bashrc`.

### Stored method (needed for click-through downloads)

Environment variables belong to your shell. When your browser launches nexus-dl for an `nxm://` link, that process sees your desktop session's environment instead, so it never sees `NEXUS_API_KEY` from `.bashrc`. If you want "Mod manager download" clicks to work, store the key with nexus-dl itself:

```bash
nexus-dl handler set-api-key
```

It asks for the key with hidden input (nothing is echoed, nothing lands in your shell history or the process list) and saves it to `~/.config/nexus-dl/api-key`, readable only by your user. `nexus-dl handler remove-api-key` deletes it again, and `nexus-dl handler status` tells you whether a key is available without ever showing it.

nexus-dl checks keys in this order: `--api-key` flag first, then `NEXUS_API_KEY`, then the stored key.

## Your first download

### Finding a collection URL

1. Go to [nexusmods.com](https://www.nexusmods.com/)
2. Pick a game (e.g., Baldur's Gate 3)
3. Click the **Collections** tab on the game's page
4. Browse or search for a collection you like
5. Click on it to open the collection page
6. Copy the URL from your browser's address bar - it looks something like:
   `https://next.nexusmods.com/baldursgate3/collections/abc123`

### Downloading

Make sure you're in the `nexus-collection-dl` directory and have the virtual environment activated (you should see `(venv)` in your prompt). Then:

```bash
nexus-dl sync "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3
```

Replace the URL with the one you copied, and `bg3` with whatever game abbreviation you like (it's just a folder name).

What this does:
- Contacts Nexus Mods to get the list of mods in the collection
- Downloads each mod file
- Extracts archives (ZIP, 7z, RAR)
- Generates a load order so mods load in the right sequence
- Saves everything to `~/mods/bg3` (a "mods" folder in your home directory)

Each collection gets its own named subfolder inside the mods directory, so you can sync multiple collections for the same game without them mixing together.

This can take a while depending on how many mods are in the collection. You'll see progress as each mod downloads. If you need to stop partway through, that's fine - re-running `sync` picks up where you left off, skipping mods that were already downloaded.

### Skipping optional mods

Some collections mark certain mods as optional. To skip those:

```bash
nexus-dl sync --skip-optional "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3
```

### File conflicts

After sync (and deploy), the tool checks for file conflicts - cases where two or more mods include the same file. If any are found, you'll see a summary listing which files overlap and which mods are involved. This is informational - the tool still works, but it helps you understand when one mod's files are being overwritten by another.

## If you have a free account

If you don't have Nexus Mods Premium, the tool does everything except the actual file download. You download the mod files yourself through the browser (Nexus shows a countdown timer for free downloads), and the tool handles the rest: figuring out which mods to get, where to find them, extracting archives, generating load order, tracking state, and deployment. Once files are local, everything works identically to Premium.

### Step 1: Sync the collection

Run the same sync command as Premium users:

```bash
nexus-dl sync "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3
```

The tool fetches the collection metadata, caches the manifest for load order generation, and shows you a table with each mod's name, filename, and a clickable URL. (`My Collection` below is the example collection subfolder `sync` creates inside `~/mods/bg3` - the folder holding `.nexus-state.json`):

```
Pending Downloads (manual)
Mod                  | Filename              | Size    | URL
Script Extender      | ScriptExtender-v2.zip | 12.3 MB | https://www.nexusmods.com/baldursgate3/mods/...
Better UI            | BetterUI-1.5.pak      | 4.1 MB  | https://www.nexusmods.com/baldursgate3/mods/...
...

Free account detected. Download the files above through your browser, save them to $HOME/mods/bg3/My Collection, then run:
  nexus-dl import "$HOME/mods/bg3/My Collection"
```

### Step 2: Download the files

Click each URL. Nexus Mods will show its standard free download page with a countdown timer. When the download starts, save the file to the collection subfolder (`$HOME/mods/bg3/My Collection` in this example - the folder containing `.nexus-state.json`, not its parent). Keep the original filename - the tool matches files by name.

**Tip:** You don't have to download everything at once. Download a few, run import, download more later - the tool tracks what's done and what's still pending.

### Step 3: Import

Once you've downloaded some or all of the files:

```bash
nexus-dl import "$HOME/mods/bg3/My Collection"
```

This scans the collection folder for files matching the pending downloads, extracts archives, updates the state, and regenerates load order. If some files are still missing, it tells you which ones.

### Step 4: Deploy

Once all mods are imported (or whenever you're ready), deploy as usual:

```bash
nexus-dl deploy "$HOME/mods/bg3/My Collection"
```

Everything works identically to Premium from here on - status, updates, undeploy, tracked mods, etc.

### Checking what's still pending

```bash
nexus-dl status ~/mods/bg3
```

Mods you haven't downloaded yet show as "Pending download" in yellow.

## Checking status

To see what's installed and whether any mods have updates available:

```bash
nexus-dl status ~/mods/bg3
```

This reads the local state file - it doesn't download anything.

## Updating mods

When mod authors release updates, you can pull them in:

```bash
nexus-dl update ~/mods/bg3
```

This checks each mod against Nexus, downloads anything that has a newer version, and regenerates the load order.

To preview what would change without actually downloading:

```bash
nexus-dl update --dry-run ~/mods/bg3
```

## Deploying to your game

Downloading mods is only half the job - the game needs to find them. The `deploy` command puts the mod files where the game expects them.

```bash
nexus-dl deploy ~/mods/bg3
```

If the game was installed through Steam, the tool auto-detects the game directory. If not (or if auto-detection fails), specify it manually:

```bash
nexus-dl deploy ~/mods/bg3 --game-dir /path/to/your/game/install
```

You only need to specify `--game-dir` once. The tool remembers it for future deploys.

### What "deploying" actually does

The tool creates **symlinks** (shortcuts) from your mod staging directory into the game's install directory. The original mod files stay in `~/mods/bg3`, but the game sees them as if they're in its own folder.

This means:
- Mod files aren't duplicated (saves disk space)
- You can "undeploy" cleanly at any time
- Updating mods automatically updates what the game sees

Deploying is tracked and reversible. If a file already exists where a mod file needs to go (your own config, a file from another manager), it is backed up byte-for-byte first, including permissions and symlink targets, into a private `.nexus-backups/` folder inside your collection directory. `plugins.txt` and the game's custom INI are tracked the same way, and your originals are put back on undeploy.

Every deployed file is fingerprinted (a SHA256 hash for copies, the exact link target for symlinks). If something changes a deployed file behind the tool's back, undeploy and redeploy refuse to touch that path and tell you which one it is, rather than deleting your edit. Fix or remove the file yourself and re-run, and it finishes the job. Each step is also journaled, so if a deploy or undeploy is interrupted (crash, power loss, Ctrl-C), the next run picks up where it left off without guessing.

Two limitations worth knowing:
- Deployments made before version 0.2.1 kept no backups, so originals those deploys replaced cannot be restored. Removing those old entries requires the deployed file to still match the staging source.
- Destination paths reached through symlinked parent directories (for example, `Data/` itself being a symlink to another drive) are refused instead of followed. This is deliberate: the tool will not mutate files outside the game directory it was pointed at.

### Preview before deploying

```bash
nexus-dl deploy --dry-run ~/mods/bg3
```

This shows what files would be placed where, without actually doing anything.

### Removing deployed mods

To undo a deployment and restore your game directory:

```bash
nexus-dl undeploy ~/mods/bg3
```

This removes every deployed symlink/copy, restores the backed-up originals (including your original `plugins.txt` and INI files), and cleans up the empty folders it created. If a path can't be removed safely (it was edited outside the tool, or its backup is missing), undeploy reports that path, keeps it tracked, and exits with an error so you know it's unfinished. Resolve the reported file and run undeploy again to complete the restore.

## Downloading with "Mod manager download" links

On each mod page, Nexus Mods shows a **Mod manager download** button next to every file. It produces an `nxm://` link, which is normally opened by a mod manager like Vortex. nexus-dl can handle these links itself.

### One-time setup

```bash
# Store your API key so browser-launched downloads can authenticate.
# The prompt is hidden: nothing is echoed, and nothing lands in your
# shell history. (A browser-launched process cannot see the
# NEXUS_API_KEY from your .bashrc.)
nexus-dl handler set-api-key

# Tell nexus-dl which synced collection folder belongs to which game.
# Use the collection subfolder created by 'nexus-dl sync' - the one
# containing .nexus-state.json, not its parent folder. Use $HOME, not a
# quoted ~: the shell only expands ~ when it is unquoted.
nexus-dl handler map starfield "$HOME/mods/starfield/My Collection"

# Make nexus-dl open nxm:// links from your browser
nexus-dl handler install
```

All three steps are opt-in and touch only your own user's settings. `handler install` writes a small `.desktop` file and remembers which app handled nxm:// links before; `nexus-dl handler uninstall` puts that back. Check everything at any time with `nexus-dl handler status`. Re-running `handler install` is safe and idempotent: it always rewrites the entry (so entries from older versions get refreshed) and probes what click-through needs (xdg-mime, notify-send, and your desktop's notification switch where one is known), warning about anything missing or switched off and never blocking the install because of it. It also runs a best-effort self-test for a reachable notification daemon; if none is found it warns and points you at the status log as the fallback feedback channel.

If you are testing this for the first time, a good disposable target is a fresh collection: run `nexus-dl sync "<collection-url>" "$HOME/mods/nxm-smoke"`, map the game to the subfolder sync prints, and work there. (If you cloned the source and installed with `uv sync`, run `uv sync --extra dev` too when you want to run the test suite.)

### What happens when you click the button

No daemon or background service runs anywhere. Your browser asks the desktop environment to open the nxm:// link; most browsers show a first-run "Launch application" confirmation for nxm:// links (allow it, and tick the remember box if you do not want to be asked again). The desktop environment looks up the registered handler and launches nexus-dl as a short-lived process with the link as its argument. The click runs headless: no terminal window opens. nexus-dl downloads that one file into the mapped collection folder, verifies it, extracts it, and updates the collection state. The outcome arrives as a desktop notification, for successes and failures alike (the registered handler asks for notifications explicitly), and then the process exits. Nothing is deployed to your game - run `nexus-dl deploy` when you're ready, exactly as usual.

Every run also appends one redacted status line to `~/.local/state/nexus-dl/nxm.log`: a timestamp, the outcome, and mod/file ids or a fixed reason code, never the link or its credentials. If you do not see a notification, check that log; it is the diagnostic fallback (see the troubleshooting section). If `handler install` warned that no notification daemon was detected, notifications will not appear in that session and the log is your primary feedback channel. The most common cause on Linux Mint is Settings > Notifications > "Display notifications" being off (GNOME hides the same switch behind "Show banners"): the download still works, only the confirmation is missing, and `handler install` warns when it finds the switch off.

What clicking the button again does depends on whether the verified archive is still on disk. The default flow extracts the archive and then deletes it, so a re-click downloads and verifies a fresh copy; this is deliberate, and the extracted files are already in place. A re-click with the archive retained triggers no download: if the entry is already marked downloaded (you ran with `--no-extract`), nexus-dl reports "already downloaded" with zero network traffic and changes nothing; if an earlier extraction failed and kept the archive, the click finishes the extraction from that archive without re-downloading it (when track-sync is enabled for the collection, a background tracked-mod sync may still make API calls). Extraction publishes files one by one rather than atomically as a set, so a rare mid-extraction failure can leave some files in place while the error is reported; re-running the extraction is safe and overwrites the partial set.

A full smoke test looks like this: finish the setup above, click **Mod manager download** once on a mod file page, accept the browser's launch confirmation, check for the desktop notification, the extracted mod in your collection folder, and the redacted success line in `~/.local/state/nexus-dl/nxm.log`. A second click re-downloads and re-verifies the file (the zero-network dedupe only shows up with a retained archive, for example after running with `--no-extract`). Run `nexus-dl handler uninstall` when you want your previous handler back.

Platform notes: the click-through flow was live-tested on Linux Mint (Cinnamon) with Firefox. GNOME, MATE, XFCE, Budgie, and KDE (including KIO-based distros like CachyOS) are expected to work through the standard xdg-mime and desktop-entry mechanisms but are untested; the README's platform support table has the details. nexus-dl never gates install on which desktop you run. Browsers installed as Flatpak or Snap generally cannot launch host desktop handlers, so click-through from those is unsupported; use a native browser build, or copy the link and run `nexus-dl nxm` yourself (see below).

### Running a link yourself (advanced)

You can also run a copied link in the terminal. To keep the link's temporary credentials out of your shell history, use a hidden read. Run `read` first, THEN paste the nxm:// link on its own line and press Enter. (If you paste the link onto the same line as the `read` command, it looks like it hung - `read` is just waiting for you to press Enter.)

```bash
read -s -r NXM_LINK
# now paste the nxm:// link and press Enter
nexus-dl nxm "$NXM_LINK" --output "$HOME/Downloads"
```

An `nxm://` link only works with `nexus-dl nxm`. Pasting an `https://` mod-page URL there is refused with an explanation of how to get the nxm:// link instead (right-click the **Mod manager download** button and copy the link address).

### When it asks you to click again

The `key` and `expires` parts of a link are temporary credentials tied to your account. If a link has expired, was copied without its credentials, or belongs to a different Nexus account than your API key, nexus-dl refuses to download and tells you to click **Mod manager download** on the website again for a fresh link. Nothing is written when that happens.

Collection links (`nxm://.../collections/...`) are not handled this way - use `nexus-dl sync` with the collection URL instead.

## Using the web UI

If you do not want to memorize terminal commands, the web UI is probably the better option for day-to-day mod management. The CLI is faster for one-off operations and scripting, but the web UI lets you do almost everything from a browser with real-time progress feedback.

To start it:

```bash
nexus-dl serve ~/mods/bg3
```

Then open your browser to `http://127.0.0.1:5000`. To use a different port:

```bash
nexus-dl serve ~/mods/bg3 --port 8080
```

### What you get

The web UI has two pages:

**Dashboard** - Shows your collection at a glance: collection name, game, installed revision, how many mods, whether updates are available, and deployment status. From here you can sync a collection (paste the URL into a text box), update, deploy, undeploy, regenerate the load order, and manage tracked-mod sync. All with buttons instead of commands.

**Mods** - Lists every installed mod with status badges (up to date, update available, removed from collection, manual, etc.), version numbers, and load phases. You can add a mod by URL or register a local mod from this page.

### Real-time progress

Long-running operations (syncing a big collection, downloading updates) stream progress to the browser in real time. You will see a progress bar and status messages as each mod downloads. This is one area where the web UI is nicer than the CLI, which just prints log lines.

### CLI vs. web UI

Both interfaces share the same code underneath, so the results are identical. The main differences:

- The CLI has `--dry-run` for deploy and update (preview without acting). The web UI does not.
- The CLI has `--no-load-order` and `--prefix` flags for more granular control.
- The web UI shows progress bars and visual status badges. The CLI prints text.

If you are just getting started, the web UI is the easier path. You only need the terminal to run `nexus-dl serve` and then everything else happens in the browser.

Press `Ctrl + C` in the terminal to stop the web server when you are done.

## The --no-extract flag

If you use a mod manager like [Stardrop](https://github.com/Jeijael/Stardrop) (for Stardew Valley) or another tool that expects raw archive files instead of extracted folders, use the `--no-extract` flag:

```bash
nexus-dl sync --no-extract "https://next.nexusmods.com/stardewvalley/collections/abc123" ~/mods/stardew
```

This downloads the mod archives but leaves them as `.zip`, `.7z`, or `.rar` files instead of extracting them. Your mod manager can then handle extraction and installation its own way.

The flag also works with `update`:

```bash
nexus-dl update --no-extract ~/mods/stardew
```

## Troubleshooting

### "python3: command not found"

Python isn't installed. See the [Checking Python](#checking-python) section above for install commands.

### "nexus-dl: command not found"

You probably need to activate the virtual environment:

```bash
cd ~/nexus-collection-dl
source venv/bin/activate
```

If that doesn't work, try re-running `./setup.sh`.

### "Permission denied" when running setup.sh

The setup script needs execute permission:

```bash
chmod +x setup.sh
./setup.sh
```

### "unrar: command not found" during sync

Some mods are packed as RAR archives. Install the `unrar` tool:

```bash
# Ubuntu/Debian
sudo apt install unrar

# Fedora
sudo dnf install unrar
```

### Rate limiting / "429 Too Many Requests"

Nexus Mods limits how many requests you can make per hour. If you hit this, wait a few minutes and try again. The tool handles rate limits automatically in most cases, but very large collections might need a retry.

### "NEXUS_API_KEY not set"

You need to set your API key. See [Setting your API key](#setting-your-api-key) above. If you set it with the permanent method, make sure you ran `source ~/.bashrc` or opened a new terminal.

If this happens when you click a **Mod manager download** link, remember that browser-launched runs cannot see your shell's environment variables at all: store the key with `nexus-dl handler set-api-key` and try the click again.

### Clicking a link seems to do nothing

Clicks run headless, so there is no terminal window to watch. The outcome arrives as a desktop notification, and every run appends one redacted status line to `~/.local/state/nexus-dl/nxm.log` - check that file first:

```bash
tail -n 5 ~/.local/state/nexus-dl/nxm.log
```

If the file has no new line after a click, the handler never ran: check `nexus-dl handler status` (entry installed, still the default, executable path valid), and remember that Flatpak/Snap browsers usually cannot launch host handlers.

If lines appear but no notification does, the click worked and only the feedback is missing. Two common causes. First, notifications are switched off in your desktop's settings: Linux Mint/Cinnamon ships with Settings > Notifications > "Display notifications" off, and GNOME hides the same switch behind "Show banners". `nexus-dl handler install` warns when it finds the switch off; the terminal fixes are:

```bash
# Cinnamon (Linux Mint)
gsettings set org.cinnamon.desktop.notifications display-notifications true

# GNOME
gsettings set org.gnome.desktop.notifications show-banners true
```

Second, the session has no notification tooling: nexus-dl needs `notify-send` (package `libnotify`), and tiling setups (i3, Sway, Hyprland) need a notification daemon like `dunst` or `mako`. The install probe warns about both. Rely on the log for outcomes either way.

### Clicking a link downloads the file again

This is by design in the default flow: the archive is deleted after a successful extraction, so there is nothing left for a second click to dedupe against, and the click downloads and verifies a fresh copy. Nothing breaks, and the extracted files are already in place. A re-click is only free when the verified archive was kept (for example with `--no-extract`): then nexus-dl reports "already downloaded" and makes no network calls.

### Mods downloaded but game doesn't see them

You need to deploy after syncing:

```bash
nexus-dl deploy ~/mods/bg3
```

Downloading puts files in a staging area. Deploying links them into the game directory.

### Something else went wrong

Start with [Discussion #61: Start here: common problems and fixes](https://github.com/scottmccarrison/nexus-collection-dl/discussions/61): it collects the common failures, and it is the place to ask for help. Feature ideas and roadmap talk belong in [Discussion #64](https://github.com/scottmccarrison/nexus-collection-dl/discussions/64). For a bug, check the [Issues page](https://github.com/scottmccarrison/nexus-collection-dl/issues) to see if someone has reported the same problem, or open a new issue with the error message you're seeing.
