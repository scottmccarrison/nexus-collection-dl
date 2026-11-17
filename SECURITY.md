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

- **Credentials are transient.** The `key`/`expires` pair from an `nxm://` link is forwarded to the download-link request only. It is never written to `.nexus-state.json`, results, logs, or error messages, and it is redacted from API errors and suppressed in HTTP debug logs. The signed download-link request refuses redirects (`allow_redirects=False`) and is timeout-bounded, so the apikey header and the credential pair can never be forwarded to an arbitrary host. The URI `user_id` is used solely as a local check that the link belongs to the configured API key's account; it is never sent to the API, and the download is refused when the account cannot be verified.
- **Desktop registration is opt-in and user-level.** `handler install` writes a `.desktop` entry to the user's XDG applications directory and registers it with `xdg-mime`. The previous default handler is recorded once and restored on uninstall only if nexus-dl is still the current default; a later user choice is never overridden. `handler uninstall` removes the entry; when a previous default was recorded and ours is still the current one, that handler is restored. When no previous handler existed, the scheme reference in the user's mimeapps data may keep pointing at the removed entry (`xdg-mime` has no command to unset a default, and clearing it would require surgical edits to shared desktop configuration). Such a dangling reference is inert per the Desktop Entry Specification: launchers ignore entries whose file no longer exists, so no click can route to the removed handler, and re-running `handler install` registers cleanly again.
- **Known limitation - argv exposure.** XDG desktop handlers receive the link as a command-line argument (`%u`), so a clicked link (including its embedded credentials) is briefly visible in the local process list while the download runs. This is inherent to how desktop environments launch handlers and cannot be avoided from inside the application. Credentials in a link are single-use and short-lived by design on the Nexus side.
- **No shell is involved.** The handler invokes `xdg-mime`, `update-desktop-database`, `notify-send`, and the optional notification-daemon probes (`gdbus`/`dbus-send`, used by the install-time self-test) as argv lists only, never through a shell, with message text passed after a `--` option separator, and executable paths are escaped per the Desktop Entry specification before being written to the entry file. `notify-send` and the daemon probes run under a hard 3-second timeout, and notification delivery is best-effort: a hung, broken, or absent notification daemon can never block a download or delay its exit beyond that bound. Other subprocess timeouts and failures surface as actionable errors; installs roll back and uninstalls preserve their recovery state.
- **Failures never claim success.** Expired, keyless, mismatched-account, or mismatched-game links fail with no writes; integrity failures preserve prior state; notification bodies are URL-sanitized.
- **The stored API key is local, 0600, and never displayed.** `handler set-api-key` reads the key from hidden stdin, so it never appears in argv, shell history, process listings, or terminal echo. It is validated without any network call and written atomically to a single plaintext key file (`~/.config/nexus-dl/api-key`, XDG-aware) with 0600 permissions; the value itself is never printed by any command. `set`/`remove`/`status` report only the stored state, the active source (flag, env, stored, or none), and the file path. The key joins the API client's error-redaction scope wherever it is used as the `apikey` header, so a hostile endpoint reflecting it cannot surface it. `handler remove-api-key` deletes only the key file; mappings and the desktop record survive. The store exists because browser-launched processes inherit the desktop session environment, not the shell's, so an exported `NEXUS_API_KEY` cannot reach a clicked nxm:// download. No daemon or background service is involved: the OS launches nexus-dl per click and the process exits when the download finishes.
- **The click log is redacted by construction.** Each `nxm` run appends one line to `~/.local/state/nexus-dl/nxm.log` (0600, XDG state dir): a UTC timestamp, an outcome token (success/warn/error), and either mod/file ids or a static reason code. Detail text is URL-stripped and rebuilt from a bounded charset before the write, so raw URIs, query credentials, key material, or free-form error text can never appear, and an injected newline cannot forge extra lines. Logging is best-effort: any log failure is swallowed and never fails a download.
