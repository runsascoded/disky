# The laptop agents as a macOS app (TCC identity)

**Status:** proposed (2026-09-29). Follow-up to `m3-site.md` Phase 3.

## Problem

Both LaunchAgents (`com.runsascoded.disk-tree.index` → `aws/laptop-scan`, `com.runsascoded.disk-tree.drain` → `aws/laptop-drain`) read `~/Library/*` and `~/.Trash`, which macOS TCC protects. TCC attributes a launchd job's file access to the job's **root executable**, so today Full Disk Access is granted to the venv's resolved Python binary (`~/.local/share/uv/python/cpython-3.14.2-…/bin/python3.14`). That identity is:
- **fragile** — a uv Python upgrade (3.13 → 3.14 did this on 2026-09-29) silently loses the grant, and the scan quietly drops 69 GiB;
- **unidiomatic** — System Settings shows "python3.14", and the picker greys out bare binaries (drag-and-drop only).

## Design

One app bundle, `disk-tree agent.app` (`CFBundleIdentifier com.runsascoded.disk-tree-agent`), installed under `~/Applications/`, whose executable is a **tiny compiled launcher** — not a script. The kernel execs the script's *interpreter* (`/bin/bash`, `python3.14`), and that is what TCC sees; a Mach-O launcher inside `Contents/MacOS/` is the responsible process, and TCC keys on the bundle. The launcher `execv`s the venv's `python` with the agent script named by its first argument:

```
disk-tree agent.app/Contents/MacOS/disk-tree-agent scan    # → .venv/bin/python aws/laptop-scan
disk-tree agent.app/Contents/MacOS/disk-tree-agent drain   # → .venv/bin/python aws/laptop-drain
```

- **Launcher**: ~30 lines of C (or Swift), built by `aws/agent-app/build` with `clang`; the worktree path baked in at build time (or read from a `Resources/config.plist`, so one build serves any checkout).
- **Signing**: TCC's grant follows the code's *designated requirement*. Ad-hoc signing (`codesign -s -`) makes that the cdhash, so every rebuild invalidates the grant — the fragility again. Sign with a stable identity: the Developer ID / Apple Development certificate if one is in the keychain, else a **self-signed code-signing certificate** created once in Keychain Access (its DR is the certificate, stable across rebuilds). Hardened runtime off (it execs an unsigned interpreter).
- **Both plists** point `ProgramArguments` at the launcher (`… /MacOS/disk-tree-agent scan|drain`); everything else in them stays.
- **Grant once**: System Settings → Full Disk Access → `+` → the app (a normal app picker entry). Survives Python, uv and worktree-venv changes. The Python-binary grant is then removed.
- **Icon + name**: an icon so the FDA row reads as ours; the app is never launched by hand (no `LSUIElement` window; `LSBackgroundOnly`).

## Scope

- `aws/agent-app/`: `main.c`, `Info.plist`, `build` (compile, bundle, sign, install to `~/Applications`), `README` (the one-time keychain certificate + the FDA click).
- Plist updates + CLAUDE.md.
- Verification: `launchctl kickstart` the scan; its total matches a shell run (414 GiB on 2026-09-29), and `~/.Trash/disk-tree/` is writable from the drainer.

## Not in scope

A signed, notarized distributable (this is one laptop); a GUI. The Tauri native app on the tool's `tauri-native-app` branch is the eventual home for a real UI; this bundle is only the TCC identity for the two agents.
