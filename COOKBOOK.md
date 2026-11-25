# Cookbook

Short, task-oriented recipes. Each one is a goal, the commands, and what
you should see. No background, no theory. For a guided walkthrough, see
[GUIDE.md](GUIDE.md). For the full command list, see [README.md](README.md).

---

## Setup

### I want to set up nexus-dl for the first time

```bash
nexus-dl setup
```

Follow the prompts. It asks where collections live and offers to install
the nxm:// click handler. When it finishes, `~/.config/nexus-dl/config.json`
exists and bare `nexus-dl` opens the shell.

### I want to change where collections live

In the shell:

```
/config root /path/to/collections
```

Or from the CLI:

```bash
nexus-dl setup --non-interactive --collections-root /path/to/collections
```

### I want to use nexus-dl in a script (no prompts)

```bash
nexus-dl setup --non-interactive --collections-root ~/nexus-collections
```

---

## Downloading

### I want to download a collection

```bash
nexus-dl sync https://next.nexusmods.com/skyrimspecialedition/collections/XXXX /path/to/collections
```

Or in the shell (just paste the URL):

```
https://next.nexusmods.com/skyrimspecialedition/collections/XXXX
```

### I want to download a single mod

```bash
nexus-dl add https://www.nexusmods.com/skyrimspecialedition/mods/12345 /path/to/collection
```

Or in the shell:

```
/add https://www.nexusmods.com/skyrimspecialedition/mods/12345
```

### I want to see what I have

```bash
nexus-dl mods /path/to/collection
```

Or in the shell:

```
/mods
```

---

## Deploying

### I want to install mods into my game

```bash
nexus-dl deploy /path/to/collection
```

Or in the shell:

```
/deploy
```

### I want to see what would be deployed without doing it

```bash
nexus-dl deploy /path/to/collection --dry-run
```

### I want to remove deployed mods from my game

```bash
nexus-dl undeploy /path/to/collection
```

### I want to deploy to a different game directory

```bash
nexus-dl deploy /path/to/collection --game-dir /path/to/game
```

---

## Managing mods

### I want to temporarily turn off a mod

```bash
nexus-dl disable --mod-id 12345 /path/to/collection
```

Turn it back on:

```bash
nexus-dl enable --mod-id 12345 /path/to/collection
```

### I want to remove a mod completely

```bash
nexus-dl remove --mod-id 12345 /path/to/collection
```

### I want to see what a mod's file id is

```bash
nexus-dl mods /path/to/collection
```

The `file_id` column is what `--mod-id` takes.

---

## Disk space

### I want to reclaim space from downloaded archives

```bash
nexus-dl gc /path/to/collection
```

This shows what can be reclaimed. Nothing is deleted until you add `--apply`:

```bash
nexus-dl gc /path/to/collection --apply
```

### I want to know how much space my archives take

```bash
nexus-dl gc /path/to/collection
```

The last line says "N archive(s), X MB reclaimable."

---

## nxm:// click-through

### I want "Mod manager download" clicks to land in nexus-dl

```bash
nexus-dl handler install
nexus-dl handler map skyrimspecialedition /path/to/collection
```

Or in the shell, the mapping is offered automatically after a sync.

### I want to see what is mapped

```bash
nexus-dl handler status
```

### I want to remove a mapping

```bash
nexus-dl handler unmap skyrimspecialedition
```

---

## The shell

### I want to open the shell

```bash
nexus-dl /path/to/collections
```

Or just `nexus-dl` if the collections root is already configured.

### I want to know what the shell can do

Type `/help` at the prompt.

### I want to switch to a different collection

```
/use 2
```

Or by name:

```
/use HS Player Homes
```

### I want to check for a newer version

```
/update
```

---

## Troubleshooting

### I mistyped the collections root during setup

```
/config root /the/right/path
```

Or from the CLI:

```bash
nexus-dl setup --non-interactive --collections-root /the/right/path
```

### I want to see the machine-readable output

Add `--json` before any command:

```bash
nexus-dl --json status /path/to/collection
```

The JSON goes to stdout. Human-readable output goes to stderr.

### I want to see what is wrong with my installation

```bash
nexus-dl health
```

### A command is failing and I want to see why

Run the command with `--json` to get structured error output:

```bash
nexus-dl --json deploy /path/to/collection 2>/dev/null
```

The `error` field in the JSON says what went wrong.
