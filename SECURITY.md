# Security Policy

## Supported versions

Only the latest version is supported. If you find a vulnerability, make sure you're on the current release before reporting.

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Instead, use one of these:

1. **GitHub Security Advisory** (preferred): Go to the [Security tab](https://github.com/scottmccarrison/nexus-collection-dl/security/advisories/new) and create a private advisory.
2. **Email**: Reach out to the maintainer directly (see GitHub profile).

Include as much detail as possible: what the vulnerability is, how to reproduce it, and what impact it has.

You should get a response within a few days. If the issue is confirmed, a fix will be prioritized and released as soon as practical.

## Scope

This policy covers the nexus-collection-dl tool itself. Issues with the Nexus Mods API, Nexus Mods website, or third-party dependencies should be reported to their respective maintainers.

## NXM link handler

Security notes for the `nxm` command and the opt-in desktop handler:

- **Credentials are transient.** The `key`/`expires` pair from an `nxm://` link is forwarded to the download-link request only. It is never written to `.nexus-state.json`, results, logs, or error messages, and it is redacted from API errors and suppressed in HTTP debug logs. The URI `user_id` is used solely as a local check that the link belongs to the configured API key's account; it is never sent to the API.
- **Desktop registration is opt-in and user-level.** `handler install` writes a `.desktop` entry to the user's XDG applications directory and registers it with `xdg-mime`. The previous default handler is recorded once and restored on uninstall only if nexus-dl is still the current default; a later user choice is never overridden. `handler uninstall` removes the entry and never leaves a stale default behind.
- **Known limitation - argv exposure.** XDG desktop handlers receive the link as a command-line argument (`%u`), so a clicked link (including its embedded credentials) is briefly visible in the local process list while the download runs. This is inherent to how desktop environments launch handlers and cannot be avoided from inside the application. Credentials in a link are single-use and short-lived by design on the Nexus side.
- **No shell is involved.** The handler invokes `xdg-mime`, `update-desktop-database`, and `notify-send` as argv lists only, never through a shell, and executable paths are escaped per the Desktop Entry specification before being written to the entry file.
- **Failures never claim success.** Expired, keyless, mismatched-account, or mismatched-game links fail with no writes; integrity failures preserve prior state; notification bodies are URL-sanitized.
