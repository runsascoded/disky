# Spec: disk-tree as a macOS app

Status: **v1 building & headless-verified** (2026-08-30) — `disk_tree.desktop` + a PyInstaller bundle (`packaging/macos/`). Remaining v1: a self-signed cert for a stable FDA grant, and a manual window-render check. Companion to the scheduled-scan work; see also memory `package-macos-app-for-tcc-identity`.

## Freezer note (settled)

py2app was the first pick but is a **dead end with this toolchain**: it requires a *framework* Python, and the uv-managed CPython 3.13 is a standalone (python-build-standalone) build — the frozen app fails with "A Python runtime could not be located" and py2app also chokes on 3.13's statically-linked `zlib` (`module 'zlib' has no attribute '__file__'`). **PyInstaller** handles the standalone Python and the native wheels; it's what shipped. Also: use **waitress**, not Flask's dev server, inside the bundle — the dev server prints its banner but never binds under PyInstaller.

## Why

The immediate driver is **TCC identity**. Today disk-tree runs as the Python interpreter, so macOS consent prompts and the Full Disk Access list show **"python3.13"**, and the FDA grant is keyed to a uv-managed interpreter path (`…/cpython-3.13.7-…/bin/python3.13`) that changes when uv upgrades Python — so the grant silently breaks. Packaging as a signed `disk-tree.app` gives:

1. **A stable, legible identity** — prompts/FDA say "disk-tree"; the grant survives rebuilds (with a stable signing identity, see below) and Python upgrades.
2. **One grant covers all its work** — a signed app's child processes (gfind, or a bundled Python) are evaluated against the *app* as the TCC "responsible process", so granting FDA to `disk-tree.app` once covers the whole scan. (This breaks under `sudo`, which re-parents the child — another reason the scheduled scan avoids `--sudo`.)
3. **A real window / nicer UX** than "start the server, open localhost".

Secondary goal (tracked, not v1): **fully runnable from an external drive, even with a 100%-full boot disk** — DB + scans + logs all on the external volume. Scans and logs already go to the external volume (`crucial-x6`); the DB stays on the boot disk by design. See "Off-boot-disk" below for the blockers.

## The stack we're wrapping

- **Backend**: Python — Flask server (`server.py`) + pandas/pyarrow/duckdb; shells out to `gfind` for the walk.
- **UI**: Vite + React, already built to static assets and served by Flask (`disk_tree/static/`).

So the app is really "a native window around the existing Flask+React, plus the ability to run scans with the app's own TCC identity."

## Options

### A. pywebview + PyInstaller  — **shipped for v1** (was: py2app; see freezer note)
- `pywebview` opens a native WKWebView window pointed at the embedded server (a free loopback port, served by **waitress** in a daemon thread, then `webview.create_window(url=…)`). Implemented in `disk_tree.desktop`.
- `PyInstaller` (`packaging/macos/disk-tree.spec`) bundles Python + deps into `disk-tree.app` with a real `Info.plist` (`CFBundleName=disk-tree`, `CFBundleIdentifier=com.runsascoded.disk-tree`). The bundle's main executable *is* the app, so TCC attributes to "disk-tree".
- **No Xcode** — PyInstaller is pip-installable; signing uses `codesign` from the **Command Line Tools**.
- Pros: smallest delta, reuses Flask+React as-is, works with the uv standalone Python (py2app does not).
- Cons: ~330 MB bundle; native wheels need `collect_all` in the spec.

### B. Electron
- Electron (Chromium + Node) hosts the React UI natively; the Python backend runs as a **sidecar subprocess** (frozen with PyInstaller) — two packaging systems.
- Pros: best web-dev workflow, cross-platform later.
- Cons: ~150 MB Chromium is ironic for a disk-space tool; two runtimes to ship and sign; the Python sidecar must be signed with inheritance so it stays under the app's TCC responsibility.

### C. Tauri (Rust + system WKWebView)
- Tiny (no bundled browser). React UI bundled; Python backend as a PyInstaller sidecar **or** hot paths ported to Rust.
- **Synergy with the perf roadmap**: Tauri's Rust host is the natural home for the native `getattrlistbulk` walker (see `specs/reflink-aware-sizing.md` / the stream-engine levers) — doing the walk *in the signed app binary* makes TCC attribution unambiguous and lands the throughput win, replacing the `gfind` subprocess.
- Cons: adds Rust; the walker port is a real project.

### D. Minimal hand-rolled `.app` (swiftc, no framework)
- A tiny Swift/WKWebView shell compiled with `swiftc` (CLT, no Xcode IDE) + hand-written `Info.plist` + `codesign`, launching the Flask server and pointing a webview at it.
- Lightest native option; most manual UI glue.

**Recommendation**: **A (pywebview + py2app)** for v1 — it gets the "disk-tree" TCC identity and a native window with the least architectural change. Keep shelling to `gfind` in v1 (it runs as the app's child → covered by the app's FDA). Treat **C (Tauri + native Rust walker)** as the v2 north star, because it unifies the app-identity goal with the getattrlistbulk performance work.

## Signing without Xcode

Full Xcode is **not** required at any stage. Install **Xcode Command Line Tools** (`xcode-select --install`) for `codesign`, `swiftc`, `notarytool`, `stapler`.

Three signing tiers, by intended reach:
- **Ad-hoc** (`codesign -s - --deep …`): works for local use, but the grant is keyed on the cdhash, which **changes every rebuild** → FDA grant breaks on each rebuild. Fine for throwaway testing, not for a tool you rebuild.
- **Self-signed certificate** (create in Keychain / `security`, no Apple account): gives a **stable designated requirement**, so the FDA grant **survives rebuilds**. This is the sweet spot for a personal tool — no Apple Developer account, no notarization, stable identity.
- **Developer ID + notarization** (`notarytool`, needs a $99/yr Apple Developer account): only if distributing to other machines (Gatekeeper). Still CLT-only, no Xcode IDE.

For our use (personal, rebuilt often, single machine or a few of Ryan's): **self-signed cert** is the target.

## v1 scope

1. `pywebview` window around the embedded Flask server (reuse `server.py` + built `static/`).
2. `py2app` build → `disk-tree.app`, `Info.plist` with `com.runsascoded.disk-tree`.
3. Self-signed codesign so the FDA grant is stable.
4. Verify: grant FDA to `disk-tree.app` once; confirm a scan reads protected folders (Photos/Desktop/…) with `error_count == 1` (htop), no prompts — same bar the interpreter-FDA scan just cleared.
5. Point the LaunchAgent at the app's executable (or keep the CLI for the schedule and use the app only for interactive use — TBD).

## v2 / open questions

- **Native walker in the app binary** (Rust via Tauri, or a Swift/C `getattrlistbulk` helper) — unifies TCC identity + throughput; retires the `gfind` subprocess. Big lift; ties into `specs/reflink-aware-sizing.md`.
- **Schedule + app**: does the LaunchAgent invoke the app bundle (so scheduled scans also carry the app identity), or keep the CLI for cron and reserve the app for interactive use? If the app, how to run it headless (no window) on a timer.
- **Off-boot-disk / 100%-full boot disk**: to run fully from the external volume, the DB must move off-boot too. Blockers today: (a) `config.py` does `makedirs(ROOT_DIR)` at import — with `DISK_TREE_ROOT` on an unmounted `/Volumes/x`, that silently creates the dir on the boot disk; (b) an explicit `DISK_TREE_ROOT` disables volume discovery *and* makes `--require-external`'s `wd == DEFAULT_SCANS_DIR` check always true (it'd always skip). Needs: guard `makedirs` behind a mount check, and teach `--require-external` to distinguish "explicit external root" from "boot fallback". SQLite on an external volume is fine when mounted; the tool should degrade gracefully (skip) when it's not.
- **App menu / status-bar item?** A menu-bar widget ("last scan: 6h ago, 418 GiB; rescan now") could be a nicer surface than a full window for a background-y tool.
