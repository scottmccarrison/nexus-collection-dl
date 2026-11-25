# Beginner's Guide

A step-by-step walkthrough for downloading and managing Nexus Mods collections on Linux. No prior command-line experience required - every command is copy-pasteable with an explanation of what it does.

## What this tool does

`nexus-dl` lets you download entire mod collections from [Nexus Mods](https://www.nexusmods.com/) on Linux. Collections are curated mod packs that other players put together - think of them as "mod playlists" where someone has already figured out which mods work well together and in what order they should load.

Normally, downloading collections requires Vortex or the Nexus Mods app, which only run on Windows. This tool does the same thing from a Linux terminal.

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

## Removing a mod

Want a mod out of your collection? For example one that conflicts, or one you simply do not want.

The commands below work on the collection folder: the named subfolder the download created inside `~/mods/bg3`, the one with the `.nexus-state.json` file in it. To avoid retyping it, point a variable at it once (use `$HOME`, and replace `My Collection` with your folder's name):

```bash
COLL=$HOME/mods/bg3/"My Collection"
```

First, see what is tracked and find the mod's `file_id`:

```bash
nexus-dl mods "$COLL"
```

You get a table of name, version, `file_id`, `mod_id`, and download status. Note the `file_id` of the mod you want to remove.

Preview the removal. This shows what would go and changes nothing:

```bash
nexus-dl remove "$COLL" --file-id 12345 --dry-run
```

When that is the right mod, remove it:

```bash
nexus-dl remove "$COLL" --file-id 12345
```

What this does:
- Removes the mod from the collection's records (every tracked file of that mod at once)
- Keeps it gone: later `sync` or `update` runs will not bring it back
- Leaves downloaded files on disk (to clean files out of your game folder, see [Removing deployed mods](#removing-deployed-mods))

Changed your mind? Re-add the mod with `nexus-dl add` (or a "Mod manager download" link) and it comes back into the collection.

You can also target the whole mod page with `--mod-id 123` instead of `--file-id 12345`. Pass exactly one of the two.

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

The tool creates **symlinks** (shortcuts) from your mod staging directory into the game's install directory. The original mod files stay in `~/mods/bg3`, but the game sees them as if they're in its own folder. Proton/Wine games always get real copies instead: the game cannot read through Linux symlinks that cross outside the Wine prefix.

This means:
- Mod files aren't duplicated (saves disk space)
- You can "undeploy" cleanly at any time
- Updating mods automatically updates what the game sees

Deploying is tracked and reversible. If a file already exists where a mod file needs to go (your own config, a file from another manager), it is backed up byte-for-byte first, including permissions and symlink targets, into a private `.nexus-backups/` folder inside your collection directory. `plugins.txt` and the game's custom INI are tracked the same way, and your originals are put back on undeploy.

Every deployed file is fingerprinted (a SHA256 hash for copies, the exact link target for symlinks). If something changes a deployed file behind the tool's back, undeploy and redeploy refuse to touch that path and tell you which one it is, rather than deleting your edit. Fix or remove the file yourself and re-run, and it finishes the job. Each step is also journaled, so if a deploy or undeploy is interrupted (crash, power loss, Ctrl-C), the next run picks up where it left off without guessing.

Two limitations worth knowing:
- Deployments made before version 0.2.1 kept no backups, so originals those deploys replaced cannot be restored. Removing those old entries requires the deployed file to still match the staging source.
- Destination paths reached through symlinked parent directories (for example, `Data/` itself being a symlink to another drive) are refused instead of followed. The tool will not mutate files outside the game directory it was pointed at.

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

Every run also appends one redacted status line to `~/.local/state/nexus-dl/nxm.log` (or `$XDG_STATE_HOME/nexus-dl/nxm.log` if `XDG_STATE_HOME` is set): a timestamp, the outcome, and mod/file ids or a fixed reason code, never the link or its credentials. If you do not see a notification, check that log; it is the diagnostic fallback (see the troubleshooting section). If `handler install` warned that no notification daemon was detected, notifications will not appear in that session and the log is your primary feedback channel. The cause hit while testing on Linux Mint was Settings > Notifications > "Display notifications" being off (on the tested machine the switch was already off; check it first if a click seems to do nothing). GNOME hides the same switch behind "Show banners": the download still works either way, only the confirmation is missing, and `handler install` warns when it finds the switch off.

What clicking the button again does depends on whether the verified archive is still on disk. The default flow extracts the archive and then deletes it, so a re-click downloads and verifies a fresh copy; the extracted files are already in place. A re-click with the archive retained triggers no download: if the entry is already marked downloaded (you ran with `--no-extract`), nexus-dl reports "already downloaded" with zero network traffic and changes nothing; if an earlier extraction failed and kept the archive, the click finishes the extraction from that archive without re-downloading it (when track-sync is enabled for the collection, a background tracked-mod sync may still make API calls). Extraction publishes files one by one rather than atomically as a set, so a rare mid-extraction failure can leave some files in place while the error is reported; re-running the extraction is safe and overwrites the partial set.

A full smoke test looks like this: finish the setup above, click **Mod manager download** once on a mod file page, accept the browser's launch confirmation, check for the desktop notification, the extracted mod in your collection folder, and the redacted success line in `~/.local/state/nexus-dl/nxm.log`. A second click re-downloads and re-verifies the file (the zero-network dedupe only shows up with a retained archive, for example after running with `--no-extract`). Run `nexus-dl handler uninstall` when you want your previous handler back.

Platform notes: the click-through flow was live-tested on Linux Mint (Cinnamon) with Firefox. GNOME, MATE, XFCE, Budgie, and KDE (including KIO-based distros like CachyOS) are expected to work through the standard xdg-mime and desktop-entry mechanisms but are untested; the README's platform support table has the details. nexus-dl never gates install on which desktop you run. Browsers installed as Flatpak or Snap generally cannot launch host desktop handlers, so click-through from those is unsupported; use a native browser build, or run `nexus-dl nxm` yourself with a link you have on hand (see below).

### Running a link yourself (advanced)

If you have an `nxm://` link on hand (the click handler flow produces them, and other tools can hand you one), you can run it in the terminal yourself. To keep the link's temporary credentials out of your shell history, use a hidden read. Run `read` first, THEN paste the nxm:// link on its own line and press Enter. (If you paste the link onto the same line as the `read` command, it looks like it hung - `read` is just waiting for you to press Enter.)

```bash
read -s -r NXM_LINK
# now paste the nxm:// link and press Enter
nexus-dl nxm "$NXM_LINK" --output "$HOME/Downloads"
```

An `nxm://` link only works with `nexus-dl nxm`. Pasting an `https://` mod-page URL there is refused with an explanation: that command runs the link the **Mod manager download** click produces, and the website doesn't offer that link as anything you can copy. For a mod page URL, use `nexus-dl add` (or `/add` in the shell) instead.

### When it asks you to click again

The `key` and `expires` parts of a link are temporary credentials tied to your account. If a link has expired, lost its `key`/`expires` pair somewhere in handling, or belongs to a different Nexus account than your API key, nexus-dl refuses to download and tells you to click **Mod manager download** on the website again for a fresh link. Nothing is written when that happens.

Collection links (`nxm://.../collections/...`) are not handled this way - use `nexus-dl sync` with the collection URL instead.

## Using the interactive shell

If you find yourself typing the same directory paths over and over, `nexus-dl shell` gives you a prompt to work in instead. It covers the everyday jobs in one place: browsing collections and games, listing and removing mods, adding a single mod, syncing a collection, and checking status.

```bash
nexus-dl shell ~/mods/starfield
```

The directory you pass is the root (`ROOT_DIR` in the help text). Leave it out and the shell uses the directory you're in. It treats a collection as the root folder itself or any immediate subfolder that holds `.nexus-state.json` (the state file `sync` creates). Nothing deeper is scanned, and a folder with a broken state file is skipped instead of blocking the list. The prompt keeps you posted on the current collection: `nexus-dl [Name]> ` once one is selected, plain `nexus-dl>` when none is. A root with exactly one collection selects that one at startup.

### A sample session

Here's a complete session. The output is real.

```
$ nexus-dl shell ~/mods/starfield
nexus-dl shell. Type /help for commands, /quit to exit.

nexus-dl> /help
Shell commands
Command                  | Description
/help                    | show this command list
/collections             | list collections under the root
/games                   | list games with collection and mod counts
/use [<index|name>]      | select the collection other commands work on, or show the current one
/mods [collection]       | list tracked mods (default: current collection)
/add <url>               | add one mod (https:// mod page or nxm:// link)
/sync [<index|name|url>] | sync a collection (default: the current collection)
/remove <file_id|mod:id> | remove a mod from the current collection
/status [<index|name>]   | tool-wide overview and health report, or one collection vs the latest revision
/quit                    | leave the shell (also /exit, or Ctrl-D)
Bare nxm:// or https:// links on a line are handled as /add.
Every command takes a bare --help (its only argument) or /help <command> for its usage.

nexus-dl> /collections
Collections under $HOME/mods/starfield
# | Name           | Game      | Mods | Pending
1 | Clean Visuals  | starfield |    2 |       0
2 | Weightless QoL | starfield |    3 |       1

nexus-dl> /use Weightless QoL
Using 'Weightless QoL' ($HOME/mods/starfield/Weightless QoL).

nexus-dl [Weightless QoL]> /mods
Mods in Weightless QoL
file_id | Name                       | Version | Download status
9034551 | Compact Inventory UI       | 0.9     | pending_download
9012345 | Faster Scanning            | 1.1     | downloaded
8842117 | Starfield Community Patch  | 2.4     | downloaded

nexus-dl [Weightless QoL]> /add https://www.nexusmods.com/starfield/mods/2885
Added 'Ship Builder Categories' as manual mod (phase 999).

nexus-dl [Weightless QoL]> /remove 9012345
Remove 'Faster Scanning' (file_id 9012345, mod_id 110477) and all its files?
this removes the mod from the collection and it stays removed through sync/update; files on disk are kept; re-add restores it
Remove 'Faster Scanning'? [yes/no]: yes
Removed 'Faster Scanning' (mod_id 110477); file(s) 9012345 left the collection.

nexus-dl [Weightless QoL]> /status
Games and collections
Game      | Collections | Total mods | Pending
starfield |           2 |          5 |       1
Total: 1 game(s), 2 collection(s), 5 mod(s), 1 pending.
Removals
Collection      | Tombstoned mods
Clean Visuals   |               0
Weightless QoL  |               1
Total removed (tombstoned): 1 mod(s). They stay removed through sync/update; re-adding a mod clears its tombstone.
Health
OK API key: stored (keystore)
OK nxm handler: installed and the default nxm:// handler
OK Notifications: enabled in this desktop's settings
OK Desktop tools: xdg-mime and notify-send are present and working
OK State files: 2 collection state file(s) readable
OK State locks: no collection state lock is in use; flock locks are released on exit, so stale lock files cannot exist

nexus-dl [Weightless QoL]> /status 2
Collection: Weightless QoL
State path: $HOME/mods/starfield/Weightless QoL/.nexus-state.json
Collection URL: https://next.nexusmods.com/starfield/collections/weightless-qol
Local: 3 mod(s), 1 pending, 1 removed (tombstoned)
Installed revision: 7
Latest revision: 8 (update available)
Mod status
Name                       | Version | Status
Compact Inventory UI       | 0.9     | pending_download
Ship Builder Categories    | 1.2     | manual
Starfield Community Patch  | 2.4     | update_available

nexus-dl [Weightless QoL]> /quit
Goodbye.
```

A few things the session doesn't show: `/games` rolls the collection list up into per-game totals, and `/mods` on its own lists the selected collection; `/mods 2` or `/mods Clean Visuals` lists another one without switching to it. `/sync` syncs a collection into the root and takes its target three ways: a collection URL (`/sync <collection-url>`), the index or name of a collection you already have on disk (`/sync 2`, `/sync Clean Visuals`), or nothing at all to resync the current collection from the URL recorded in its state (with no recorded URL it asks for one explicitly). Those two forms (index/name and bare resync) update that collection's own directory, even if you renamed its folder since the last sync; the URL form makes a new directory under the root, named from the API. `/add` also takes an `nxm://` link if you have one, and free accounts get the manual download instructions from `/add`, same as the plain `add` command. Every command also answers a bare `--help` as its only argument (or `/help <command>`) with its usage and one example instead of running. Anything typed that isn't a command or a link gets a hint instead of a crash, and the shell keeps running after errors and after Ctrl-C (which just cancels the line you were typing):

```
nexus-dl> hello world
Error: Unknown input. For the command list, try /help. Bare nxm or https links are handled as /add.
```

### Picking a collection with /use

Most commands work on one selected collection, and `/use` picks it in this order, first match wins:

- **An exact name.** Case-sensitive first, so a collection named `2024` is selected by name even though it looks like an index.
- **An index.** The `#` column from `/collections`, ASCII digits only; a unicode digit is never read as an index.
- **A name, case-insensitively.** A unique match is used. If the name fits more than one collection, the shell refuses to guess and lists the indexes:

```
nexus-dl> /use DUP
Error: 'DUP' matches 2 collections (names differ only by case); pick one by index:
  1: Dup [starfield]
  2: dup [starfield]
```

An unknown name gets `no collection named '...'` plus the same listing, so you can always fall back to an index. `/use` on its own changes nothing: it reports the current selection (or `none selected`) and prints the collection list. An explicit `/use` remembers the selection for that root in `.nexus-shell.json` (one value: the collection's directory name, nothing else), and the next shell on that root restores it; a stale or corrupt file is ignored.

### Checking status with /status

`/status` comes in two forms, and the sample session above shows both.

Bare `/status` is the tool-wide report: a per-game overview with totals, the removal tombstone counts per collection, and a short health check. It reads local state only, never touches the network, and works before you `/use` anything. It also stays tool-wide when a collection is selected; if you want one collection, name it. The health check prints one line per check (`OK`, `WARN`, or `ERROR` with a short fix) covering the API key (source only, never the value), the nxm handler and its default, your desktop's notification switch (with the exact `gsettings` command when it's off, and nothing at all when the desktop is unknown), `xdg-mime` and `notify-send`, unreadable state files that the listings skip, and the collection write locks. A check that fails renders as a line, not a crash: `/status` always finishes. One note on locks: the writer lock is a kernel flock that releases itself when a process exits, so a leftover lock file is normal and there is no such thing as a stale one here; the report only says which collections a running operation is using.

`/status <index|name>` is the per-collection form, and it does hit the API: it compares your installed revision and each mod against the latest revision (the `Mod status` table), and adds the local picture next to it: the mod, pending, and tombstone counts, the collection URL recorded at the last sync, and the state file path.

### Removing a mod

`/remove` takes one id: a bare number is the `file_id` from the `/mods` table, and `mod:<id>` is the mod_id (the id of the mod's page on Nexus; the confirmation line shows both). Either way it removes the whole mod, meaning every file entry that belongs to that mod page, not just the one file you named. Before anything happens, the shell prints what will go and asks for confirmation. Only `yes` confirms; "no", Ctrl-C, Ctrl-D, or anything else aborts with `Cancelled; nothing was removed.`

What removal actually does, spelled out because "remove" can mean several things:

- **State only.** The mod's entries are deleted from `.nexus-state.json` and a tombstone (the `removed_mods` key) is written for its mod page. Files on disk are never touched: extracted content and kept archives stay exactly where they are. A collection's extraction tree is merged into one folder where mods share and overwrite paths, so payload files cannot be cleanly attributed back to a single mod. Cleaning up deployed files is `undeploy`'s job.
- **The removal sticks.** `sync` and `update` honor the tombstone and skip the mod (the summary reports `N removed mod(s) skipped`), so a later collection revision can't quietly reinstall it. The tombstone keys on the mod page (mod_id), not on file_id, so new file ids in a newer revision still match.
- **Re-adding restores it.** Adding the mod again (link, `add`, or `import`) clears the tombstone and brings the mod back. The shell says so honestly in that case, on every add path (an `https://` URL, an `nxm://` link, or the free-account manual download): `Re-added '<name>' to '<collection>'; it is a collection member, not a manual mod`, because the next sync folds it back into the collection-managed set either way.
- **One downgrade caveat.** Older nexus-dl versions don't know about the `removed_mods` key: they ignore it and drop it the next time they save state, so running an older build can resurrect removed mods at the next sync or update.

Removing an id that isn't in `/mods` fails with `No mod file with file_id ... in ...` and changes nothing.

### Pasting nxm:// links

Pasting an `nxm://` link on a line by itself is the same as `/add` with that link. Where you get one: the **Mod manager download** click produces it for the handler flow, or another tool can hand you one; the website itself doesn't offer the link as anything you can copy. The shell never echoes the link back: everything it prints goes through a redactor that covers the link and its `key`/`expires` credentials, so even error messages can't reflect them. A link that expired or belongs to a different account gets `Click 'Mod manager download' on the Nexus website again, then retry.` and nothing is written.

### If a link click seems to do nothing

If you're pasting links here because clicking **Mod manager download** in the browser produced nothing visible, that's usually the click-through side rather than the shell: desktop notifications are switched off, or the handler isn't installed. Start with "Clicking a link seems to do nothing" in [Troubleshooting](#troubleshooting); the short version is to check `~/.local/state/nexus-dl/nxm.log` first, because a new line there means the click ran and only the feedback is missing.

### Reacting to clicks from the shell

When `nexus-dl shell` is open and you click **Mod manager download** in your browser, the shell notices while you sit at the prompt - no keystroke needed. You'll see something like:

```
Click activity while the shell was running (2 events):
  'SkyUI' (file 456) downloaded into 'Clean Visuals'
  'SKSE64' (file 111) is already downloaded in 'Clean Visuals'
```

Then, for each collection that got a new file, the shell asks once:

```
Deploy 'Clean Visuals' now? [yes/no]:
```

Answer `yes` and it shows what would be deployed (file counts and the game directory), asks again, and only then touches the game directory. Answer `no` at any point and nothing happens. Three clicks on the same collection produce one report and one prompt, not three.

A click on a **FOMOD mod** is different: those need option choices before anything can be extracted, so the mod waits and the shell offers the choices instead:

```
Click activity while the shell was running (1 events):
  'XPMSE' (file 456) needs FOMOD option choices in 'Clean Visuals' (run import --fomod-option to install it)
1 FOMOD mod in 'Clean Visuals' need option choices. Choose now? [yes/no]:
```

Answer `yes` and the installer's groups are shown one at a time, exactly as `add` shows them at a terminal (numbered options, the recommended one marked, Enter for none where a group allows it). The picks are recorded and the mod installs right there, then the deploy question follows as usual. Press **Ctrl-C** at any prompt to abort: the mod stays waiting, nothing is installed from a partial set of answers, and the shell keeps running. Answer `no` and the mod stays waiting: `nexus-dl import <collection> --fomod-option 'Group:Option'` picks it up later, and nothing is ever installed from a guess.

The shell reads a small events file each collection keeps at `<collection folder>/.nexus-events.jsonl`. You never need to touch it. Only collections under the shell's root folder are visible: if you clicked a mod into a collection the shell can't see, use `/collections` or `/mods` to check. Events that arrived before the shell started are not replayed; you'll see only clicks that happened while it was running.

You can also deploy at any time without waiting for a click, using `/deploy` (or `/deploy 2` or `/deploy Clean Visuals` to pick a collection). It works the same way: dry-run counts, a confirmation, then the deploy.

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

If lines appear but no notification does, the click worked and only the feedback is missing. Two common causes. First, notifications are switched off in your desktop's settings: on the Linux Mint (Cinnamon) machine this was tested on, Settings > Notifications > "Display notifications" was already off, and GNOME hides the same switch behind "Show banners". Check the switch first if a click seems to do nothing. `nexus-dl handler install` warns when it finds the switch off; the terminal fixes are:

```bash
# Cinnamon (Linux Mint)
gsettings set org.cinnamon.desktop.notifications display-notifications true

# GNOME
gsettings set org.gnome.desktop.notifications show-banners true
```

Second, the session has no notification tooling: nexus-dl needs `notify-send` (package `libnotify`), and tiling setups (i3, Sway, Hyprland) need a notification daemon like `dunst` or `mako`. The install probe warns about both. Rely on the log for outcomes either way.

### Clicking a link downloads the file again

In the default flow the archive is deleted after a successful extraction, so a second click downloads and verifies a fresh copy. Nothing breaks, and the extracted files are already in place. A re-click is only free when the verified archive was kept (for example with `--no-extract`): then nexus-dl reports "already downloaded" and makes no network calls.

### Mods downloaded but game doesn't see them

You need to deploy after syncing:

```bash
nexus-dl deploy ~/mods/bg3
```

Downloading puts files in a staging area. Deploying links them into the game directory.

### Something else went wrong

Start with [Discussion #61: Start here: common problems and fixes](https://github.com/scottmccarrison/nexus-collection-dl/discussions/61): it collects the common failures, and it is the place to ask for help. Feature ideas and roadmap talk belong in [Discussion #64](https://github.com/scottmccarrison/nexus-collection-dl/discussions/64). For a bug, check the [Issues page](https://github.com/scottmccarrison/nexus-collection-dl/issues) to see if someone has reported the same problem, or open a new issue with the error message you're seeing.
