# disk-tree — Tauri v2 native macOS app

The **v2 north star** from `specs/macos-app.md` (Option C): a tiny native window
(system WKWebView, no bundled browser) around the existing Flask+React UI, with
the `gfind` subprocess replaced by a native `getattrlistbulk` walker compiled
into the signed app binary. See `specs/tauri-native-app.md` for the full plan and
status.

## Layout

```
apps/tauri/
  crates/dt-walker/   # native getattrlistbulk walker (Phase 1) — gfind-compatible stream
  src-tauri/          # Tauri v2 host (Phase 2): spawns the Python backend, opens the window
```

- **`dt-walker`** is a workspace dependency of `src-tauri`, so the walk runs
  *inside* the signed app binary — TCC attributes to "disk-tree" with no child
  process. See `crates/dt-walker/README.md`.
- The host also points the spawned Python backend at the bundled `dt-walker`
  binary via `DISK_TREE_WALKER`, so its scans use the native walker too.

## Toolchain

- Rust + cargo (`rustup`).
- `tauri-cli` v2: `cargo install tauri-cli --version "^2.0" --locked`.
- Xcode **Command Line Tools** (`codesign`), Node + pnpm (UI build).

## Build & run

```bash
# 1. Build the UI the host wraps (frontendDist = ../../../ui/dist).
( cd ../../ui && pnpm install && pnpm build )

# 2. Dev: opens the window, spawns `disk-tree-server` on a free loopback port.
#    Needs `disk-tree-server` on PATH (the project venv provides it).
cargo tauri dev            # from apps/tauri/

# 3. Bundle + sign → src-tauri/target/release/bundle/macos/disky.app
cargo tauri build --bundles app
```

Signing uses the stable self-signed identity **`disk-tree-selfsigned`**
(`bundle.macOS.signingIdentity` in `tauri.conf.json`), so the Full Disk Access
grant survives rebuilds and TCC shows "disk-tree" — same identity and bundle id
(`com.runsascoded.disk-tree`) as the v1 PyInstaller app.

### Environment seams

- `DISK_TREE_SERVER_CMD` — override the backend command (default `disk-tree-server`;
  a packaged build points this at the PyInstaller sidecar).
- `DISK_TREE_WALKER` — path to the `dt-walker` binary the backend should use
  instead of `gfind` (the host sets this automatically when it finds the bundled
  walker next to its executable).

## Menu bar, agents, login item

`disky` is a menu-bar app (no Dock icon): scan status, Full Disk Access status, Scan now, a window on
disk.rbw.sh (`DISKY_URL`), and toggles for **Scheduled scans** and **Open at login**.

Scheduled work runs as the app's bundled LaunchAgents
(`Contents/Library/LaunchAgents/com.runsascoded.disky.{scan,drain}.plist`, registered with
`SMAppService`, listed as "disky" in Login Items), so the app's Full Disk Access grant covers it.
A bundled plist is static, so the per-user part (command, env, log name) lives in
`~/.config/disk-tree/disky.json`:

```json
{"jobs": {"scan":  {"command": ["…/.venv/bin/python", "…/aws/laptop-scan"], "env": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}, "log": "index"},
          "drain": {"command": ["…/.venv/bin/python", "…/aws/laptop-drain"], "log": "drain"}}}
```

```bash
disky job scan                      # what the scan agent runs (06:00, 18:00); output → ~/Library/Logs/disk-tree/<log>.{out,err}.log
disky agents register|unregister|status [scan|drain]
disky login-item on|off|status
disky agent -- CMD ARGS…            # run any command as the app's child (its FDA grant covers it)
disky probe                         # read TCC-protected dirs in-process; exit 3 if any denied
scripts/agentctl install            # target/…/disky.app → ~/Applications (stable grant path)
scripts/agentctl check              # the probe, as launchd jobs: is FDA granted to the app?
scripts/agentctl adopt              # hand-written agents → disky.json + bundled agents (`unadopt` reverts)
scripts/agentctl status
```

## Status / what's left

- **Done**: native walker at gfind parity (Phase 1); host compiles, opens a window
  on the backend, links the walker in, signs with `disk-tree-selfsigned` (Phase 2/4).
- **Remaining**: bundle the Python backend as a PyInstaller **sidecar**
  (`externalBin`) so the app is self-contained (today it spawns `disk-tree-server`
  from PATH); ship the `dt-walker` binary as a bundle resource; wire the in-process
  walker stream directly into aggregation (vs. the subprocess seam). See the spec.

## Icons

`src-tauri/icons/` are generated from `src-tauri/app-icon.png` via
`cargo tauri icon app-icon.png`. **The current icon is a placeholder** — replace
`app-icon.png` and regenerate for real branding.
