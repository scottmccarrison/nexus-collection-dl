# Beginner's Guide

A step-by-step walkthrough for downloading and managing Nexus Mods collections on Linux. No prior command-line experience required - every command is copy-pasteable with an explanation of what it does.

## What this tool does

`nexus-dl` lets you download entire mod collections from [Nexus Mods](https://www.nexusmods.com/) on Linux. Collections are curated mod packs that other players put together - think of them as "mod playlists" where someone has already figured out which mods work well together and in what order they should load.

Normally, downloading collections requires Vortex or the Nexus Mods app, which only run on Windows. This tool does the same thing from a Linux terminal (or from a local web page in your browser if you prefer clicking over typing).

It works with any game on Nexus Mods - Baldur's Gate 3, Starfield, Cyberpunk 2077, Skyrim, Stardew Valley, and everything else.

## What you need before starting

1. **A Nexus Mods Premium account** - The free tier doesn't allow automated downloads through the API. You need Premium to use this tool. (This is a Nexus Mods limitation, not ours.)

2. **Python 3.10 or newer** - This comes pre-installed on most Linux distributions. We'll check in a moment.

3. **A Nexus Mods API key** - This is a secret code that lets the tool download on your behalf. We'll get this together below.

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

You should see a list of commands like `sync`, `update`, `deploy`, etc.

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

This can take a while depending on how many mods are in the collection. You'll see progress as each mod downloads.

### Skipping optional mods

Some collections mark certain mods as optional. To skip those:

```bash
nexus-dl sync --skip-optional "https://next.nexusmods.com/baldursgate3/collections/abc123" ~/mods/bg3
```

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

## Using the web UI

If you prefer a visual interface over typing commands, the tool includes a local web dashboard:

```bash
nexus-dl serve ~/mods/bg3
```

This starts a local web server. Open your browser and go to:

```
http://127.0.0.1:5000
```

From the dashboard you can sync collections, update mods, deploy, and manage your mod list - all by clicking buttons instead of typing commands.

To use a different port:

```bash
nexus-dl serve ~/mods/bg3 --port 8080
```

Press `Ctrl + C` in the terminal to stop the web server when you're done.

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

### Mods downloaded but game doesn't see them

You need to deploy after syncing:

```bash
nexus-dl deploy ~/mods/bg3
```

Downloading puts files in a staging area. Deploying links them into the game directory.

### Something else went wrong

Check the [Issues page](https://github.com/scottmccarrison/nexus-collection-dl/issues) on GitHub to see if someone has reported the same problem, or open a new issue with the error message you're seeing.
