# Spec: disk-tree Tauri v2 native macOS app

Status: **in progress** (2026-09-08; roadmap extended 2026-09-30) — greenfield Option C from
`specs/macos-app.md`. This is the reviewable plan; it's kept in sync with the code and moves to
`specs/done/` only when v2 is real (signed `.app`, native walker feeding scans end-to-end, the
laptop's scheduled scans running under the app's identity).

**2026-09-30:** the app is now also the laptop's *agent identity* (Phase 5, absorbing `m3`'s
`macos-agent-app.md`, now in `specs/done/` as superseded) and the home of *whole-machine
coverage* (Phase 6). Branch `tauri-native-app` merged `cloud` at `e1967dd`.

## Why v2 (recap of `macos-app.md` Option C)

v1 (shipped, `packaging/macos/`) is pywebview + PyInstaller wrapping the Flask+React UI, still
shelling to `gfind`. v2 replaces the `gfind` subprocess with a **native `getattrlistbulk(2)`
walker compiled into the signed app binary**. Three wins, all from that one move:

1. **Unambiguous TCC identity** — the walk *is* the app (no child process); FDA attribution to
   "disk-tree" is exact, not "the app's child `gfind`".
2. **No `brew install findutils` dependency** — the walker is built in.
3. **Throughput** — `getattrlistbulk` is the one lever above the ~42K/s single-thread APFS
   metadata-lock ceiling (`specs/reflink-aware-sizing.md`): one syscall returns a whole
   directory's entries *with* their attributes, and can capture `ATTR_CMNEXT_PRIVATESIZE`
   (dedup-aware sizing) in the same pass later.

## Architecture

### Directory layout

```
apps/tauri/
  README.md               # build/run instructions
  Cargo.toml              # cargo workspace (walker + src-tauri members)
  crates/
    dt-walker/            # standalone native walker crate (Phase 1)
      Cargo.toml
      src/
        lib.rs            # Walker: getattrlistbulk enumeration → Record stream
        attrlist.rs       # getattrlistbulk(2) FFI + variable-buffer parsing
        record.rs         # Record + gfind-compatible `%y %b %T@ %p\0` serialization
        bin/dt-walker.rs  # CLI: `dt-walker <root>` (drop-in for the gfind subprocess)
  src-tauri/              # Tauri v2 host (Phase 2)
    Cargo.toml
    tauri.conf.json
    build.rs
    src/main.rs           # spawns Python sidecar (waitress), opens WKWebView window
```

The React UI is unchanged: Tauri loads the already-built `ui/dist` (bundled as the app's
`frontendDist`). The Flask backend ships as a **PyInstaller sidecar** (same freeze recipe as
v1), spawned by the Rust host on a loopback port, exactly as `disk_tree.desktop` does today —
so the whole `/api/*` contract and SSE progress stream work with zero server changes.

### How the walker's output reaches Python aggregation (the seam)

The native walker emits **byte-for-byte the same stream `gfind` produces**:
`%y %b %T@ %p\0` (null-terminated records, space-separated, `%p` last so paths with spaces
survive `split(' ', 3)`). Field semantics reproduced exactly:

| field | gfind meaning | native source |
|-------|---------------|---------------|
| `%y`  | type char `f`/`d`/`l` (else the raw letter) | `ATTR_CMN_OBJTYPE` → `VREG`→`f`, `VDIR`→`d`, `VLNK`→`l`, other mapped like GNU find |
| `%b`  | 512-byte blocks allocated to the path | `st_blocks` semantics — see "%b parity" |
| `%T@` | mtime as epoch seconds (float ok; parser does `int(float(...))`) | `ATTR_CMN_MODTIME` timespec → `sec` |
| `%p`  | absolute path | root + accumulated relative path |

Because the stream is identical, the seam into Python is minimal and **additive** (Phase 3):
`src/disk_tree/backends/local.py` gains an opt-in `DISK_TREE_WALKER=<path-to-binary>` env var.
When set, `LocalBackend.list` runs `<walker> <root>` instead of building the `gfind` argv, and
feeds its stdout to the *unchanged* null-record parser (`run_gfind`; the gfind path stays the
default). No aggregation code changes.

This keeps two properties the task requires: (a) the gfind path is never removed, only an
alternative added; (b) the entire existing Python pipeline (parse → DataFrame → `aggregate`)
consumes the native stream unchanged, so the native walker is a true drop-in.

### `%y`/`%b`/`%T@` parity details

- **`%y`**: `gfind.py` maps `f`→file, `d`→dir, and passes any other single letter through
  (`l` for symlink stays `l`). The walker emits `f`/`d`/`l` and — for the rare block/char
  device, socket, fifo — the same letters GNU find uses (`b`/`c`/`s`/`p`), so the passthrough
  branch behaves identically. Type comes from `ATTR_CMN_OBJTYPE` (`fsobj_type_t`).
- **`%b`**: GNU find's `%b` is `st_blocks` (512-byte units). getattrlistbulk exposes
  `ATTR_FILE_ALLOCSIZE` (bytes) for file forks; we request it for files and emit
  `ALLOCSIZE / 512`, treating **dirs/symlinks as 0 blocks** (APFS directories report
  `st_blocks == 0`, which is what `gfind %b` prints). **Verified empirically (Phase 1):**
  `ALLOCSIZE/512` matches `gfind`'s `st_blocks` *exactly* — **0 `%b` mismatches across ~1.06M
  files** (480,323 in `~/c/oa/marin` + 579,581 in `~/Library/Caches`). No resource-fork /
  compressed-file divergence surfaced; the `lstat`-fallback contingency was not needed.
  - **Layout gotcha (found & fixed in Phase 1):** `FSOPT_PACK_INVAL_ATTRS` does **not**
    zero-pack the *file* attribute group for non-file entries — a directory entry omits
    `ATTR_FILE_ALLOCSIZE` entirely, so blindly reading its buffer slot yields garbage (a dir
    printed `%b = 603992378`). The parser now consults each entry's `returned` attribute_set
    (`fileattr` word) and reads allocsize only when the `ATTR_FILE_ALLOCSIZE` bit is set;
    dirs/symlinks get 0. Entries are advanced by the leading `length` field, so a shorter
    (allocsize-less) entry is walked correctly.
- **`%T@`**: `gfind` prints `<sec>.<frac>`; the parser only does `int(float(...))`, so the
  fractional part is discarded. The walker prints integer seconds — the parsed values are
  identical.

### CloudStorage exclusion

`local.py` prunes `~/Library/CloudStorage` (and `/Library/CloudStorage`) via
`-path … -prune`. The walker takes an exclude list (default those two) and skips any directory
whose absolute path matches, before recursing — same effect, no network-blocking `stat`.

### Error handling

`gfind` continues past unreadable dirs and prints `find: '<path>': Permission denied` to
stderr; `run_gfind` counts those. The walker mirrors this: on `EACCES`/`EPERM` opening or
reading a directory, it emits the identical `dt-walker: '<path>': Permission denied` line to
stderr (matched by the existing `PERMISSION_DENIED_RE`, which accepts `g?find:` — widened to
also accept the walker's prefix in Phase 3) and continues. Record count, not a crash.

## Python backend packaging

**PyInstaller sidecar** (not embedded). Reasons: v1's freeze recipe (`packaging/macos/
disk-tree.spec`) already works with the uv-managed standalone CPython 3.13 and the native
wheels (pyarrow/duckdb); Tauri's sidecar mechanism (`externalBin`) is built for exactly this;
and it keeps the Python contract identical to v1 (waitress on loopback). The sidecar binary is
signed with inheritance so it stays under the app's TCC responsibility.

## Signing / bundling

Reuse the **existing stable self-signed identity** `disk-tree-selfsigned`
(`security find-identity -v -p codesigning` → present) so TCC attributes to "disk-tree" and the
FDA grant survives rebuilds. Bundle id **`com.runsascoded.disk-tree`** (same as v1 — one FDA
grant, one identity across v1/v2). Tauri's bundler produces the `.app`; we sign with `codesign
--sign disk-tree-selfsigned` (Tauri can invoke it via `bundle.macOS.signingIdentity`, or we
sign post-build like `build.sh`). Acceptance: `codesign -dvvv` shows
`Authority=disk-tree-selfsigned` and `Identifier=com.runsascoded.disk-tree`.

Because the walker is *inside* the app binary (a workspace crate linked into the Tauri host),
the walk carries the app's identity with no child-process caveat — the core v2 win.

## What stays gfind-compatible

- The record stream format (`%y %b %T@ %p\0`) — verbatim.
- The Python parser (`run_gfind`) — unchanged; only the *source command* is swapped behind an
  env var.
- The `aggregate()` pipeline, DataFrame columns, parquet layout — untouched.
- The gfind path remains the default; the native walker is opt-in until proven at parity.

## Toolchain installed

- Rust 1.93 / cargo 1.93 (pre-existing).
- `tauri-cli` **v2.11.4** — installed via `cargo install tauri-cli --version "^2.0" --locked`.
- Command Line Tools (pre-existing) for `codesign`; `disk-tree-selfsigned` identity (pre-existing).
- Node 26 / pnpm 10 (pre-existing) for the UI build.

## Phases

- **Phase 0** — this spec. ✅
- **Phase 1** — `dt-walker` crate + CLI; benchmark & parity-diff vs `gfind`. ✅
  - **Result:** byte-exact parity (0 mismatches on ~1.06M files across two trees); **1.58x**
    faster on `~/Library/Caches` (10.1s vs 16.0s, 580K files), **2.73x** on `~/c/oa/marin`
    (3.34s vs 9.10s, 480K files), both warm-cache. Harness: `crates/dt-walker/parity.py`.
- **Phase 2** — Tauri v2 shell around `ui/dist`, Python sidecar. ✅ (host + window; sidecar TODO)
  - `apps/tauri/src-tauri`: `disk-tree-app` opens a system-WKWebView window, spawns the Python
    backend on a free loopback port, and links `dt-walker` in. **Runtime smoke verified:** the
    window loaded the UI and made live API calls (`GET /` → 200, assets, `/api/scans`,
    `/api/backend/available`, the progress SSE stream — all 200). Backend is spawned from PATH
    (`disk-tree-server`) for now; the self-contained **PyInstaller sidecar** is the remaining
    piece (see below).
- **Phase 3** — `DISK_TREE_WALKER` seam in `local.py`; scan end-to-end through the native walker. ✅
  - `LocalBackend.list` swaps the source command to `dt-walker` when `DISK_TREE_WALKER` is set,
    feeding the *unchanged* `run_gfind` null-record parser; `PERMISSION_DENIED_RE` widened to
    match the walker's `dt-walker:` prefix. **Verified:** a full `disk-tree index` (parse →
    aggregate → parquet → `du`) through the walker yields a **byte-identical scan** to the
    gfind path (`tests/test_backends.py::test_dt_walker_seam_matches_gfind`, exact DataFrame
    equality; skipped when the binary isn't built).
- **Phase 4** — sign + bundle with `disk-tree-selfsigned`; FDA-grant verification steps. ✅ (signing;
  FDA grant is Ryan's GUI step)
  - `cargo tauri build --bundles app` → `target/release/bundle/macos/disk-tree.app` (**6.4 MB** —
    no bundled browser, vs v1's ~330 MB). `codesign -dvvv` confirms `Authority=disk-tree-selfsigned`
    and `Identifier=com.runsascoded.disk-tree`; `codesign --verify --deep --strict` passes
    ("satisfies its Designated Requirement"). Notarization intentionally skipped (personal use).
  - **FDA grant + protected-folder verification** (Ryan's GUI step):
    1. Move/keep `disk-tree.app` at a stable path (rebuilds keep the same cdhash-independent
       Designated Requirement, so the grant survives).
    2. System Settings → Privacy & Security → Full Disk Access → **+** → add `disk-tree.app`.
       The list entry reads **"disk-tree"** (not "python3.13"), the whole point of v2.
    3. Launch the app; run a scan over a protected folder (Desktop/Documents/Downloads/Photos).
       It should read them with no per-file prompts and a low `error_count` — the same bar the
       interpreter-FDA scan cleared, but now keyed to the stable app identity.

- **Phase 5** — scheduled scans under the app's TCC identity. ✅ code, ⏳ Ryan's FDA grant + routing.
  See "Scheduled scans" below.
- **Phase 6** — whole-machine coverage (not just `~`). ⏳ walker half done. See "Whole-machine coverage".
- **Phase 7** — menu-bar presence ✅, `SMAppService`-registered agents ✅, login item ✅,
  template icon ✅; in-app sign-in ✅ (verified on dev). The window loads disk.rbw.sh (Ryan agreed 2026-09-30). See "Menu bar".

## Scheduled scans (Phase 5)

The laptop's two LaunchAgents (`com.runsascoded.disk-tree.index` → `m3`'s `aws/laptop-scan`,
`com.runsascoded.disk-tree.drain` → `aws/laptop-drain`) read TCC-protected trees (`~/.Trash`,
`~/Library/{Mail,Messages,Safari,Containers,…}`). TCC charges a launchd job's reads to the job's
*root executable*, so today Full Disk Access is granted to the venv's resolved `python3.x`: a uv
Python upgrade silently drops it (3.13 → 3.14 on 2026-09-29 cost a scan 69 GiB), and the FDA row
reads "python3.14".

`m3`'s `macos-agent-app.md` proposed a separate tiny `disk-tree agent.app` with a C launcher. This
bundle already is one: same signed identity, no second app. So:

- **`disky agent [--] CMD…`** (`src-tauri/src/agent.rs`), dispatched on argv *before*
  Tauri/AppKit start (no window, no Dock icon). It **spawns** CMD as a child and exits with its
  status, forwarding TERM/INT/HUP. Correction to `macos-agent-app.md`, which said the launcher
  `execv`s python: an `exec` replaces the signed image with the interpreter's, and the job's
  responsible code is `python3.x` again.
- **`disky probe`** reads the protected dirs in-process and prints `ok`/`denied`/`absent`
  per dir; exit 0 iff none denied.
- **`apps/tauri/scripts/agentctl`**: `install` (bundle → `~/Applications/disky.app`, a stable
  path), `check` (runs `probe` and `agent -- /bin/ls ~/Library/Mail` *as launchd jobs*, so TCC sees
  the app rather than the terminal), `route LABEL…` / `unroute LABEL…` (prefix or strip the
  plist's `ProgramArguments` with the wrapper, back it up, reload), `status`.
- **Verified 2026-09-30:** with no grant to the app, both launchd probes are denied (the in-process
  read and the `/bin/ls` child), while the same probe from a terminal with FDA reads everything.
  So TCC charges the job, children included, to `disk-tree.app`. Signature:
  `designated => identifier "com.runsascoded.disk-tree" and certificate leaf = H"e055a22e…"`
  (the `disk-tree-selfsigned` cert), so the grant survives rebuilds.

**Cut-over (Ryan + `m3`):**
1. System Settings → Privacy & Security → Full Disk Access → **+** → `~/Applications/disky.app` (done 2026-09-30, as `disk-tree.app`; carried over).
2. `apps/tauri/scripts/agentctl check` → `FDA: granted`.
3. `agentctl route index drain`, then `launchctl kickstart gui/$UID/com.runsascoded.disk-tree.index`;
   its total must match a shell run (Σalloc over `~` was 398 GiB on 2026-09-30).
4. Remove the python3.x FDA row. Future uv/Python changes no longer matter.

**Build note:** macOS 27's dyld rejects Cargo-stripped proc-macro dylibs ("mis-aligned LINKEDIT
string pool" → E0463 "can't find crate for `phf_macros`"); `[profile.release.build-override]
strip = false` in `apps/tauri/Cargo.toml` fixes it (Rust 1.93, ld-27037). 1.98 may not need it.

## Whole-machine coverage (Phase 6)

Scans cover `/Users/ryan` only, so the map can't explain the rest of the disk. Measured 2026-09-30
(460 GiB container, 23 GiB free):

| where | GiB | how it's reachable |
|---|---|---|
| `~` (walk, apparent) | 398.3 | today's scan; clone-overcounted (Σprivate 288) |
| rest of Data + System (walk) | 58.8 | `dt-walker --one-fs /`: `/System/Library` 16.7, `/Applications` 15.1, `/opt` 13.0, `/private/var` 8.6, `/Library` 3.4, … |
| Preboot volume | 20.2 | not walkable usefully; `diskutil apfs list -plist` → `CapacityInUse` |
| VM volume (swap) | 12.0 | same |
| Recovery ×2, Update | 6.4 | same |
| OS-update snapshots | ? | `diskutil apfs listSnapshots /`: 3, not purgeable, incl. `MSUPrepareUpdate` |

**Walk:** `dt-walker --one-fs /` walks the sealed System volume plus the Data volume through its
firmlinks (`/usr/share/firmlinks`; they aren't mount points, so `--one-fs` follows them while
skipping `/System/Volumes/*`, `/Volumes/*`, `/dev`, nullfs). 8.74M entries in 144 s, **285
unreadable dirs** (root-only: `/private/var/{folders,db,spool}`, `/System/Library/Templates`).
Paths keep their usual spelling (`/Users/ryan/…`), so a `/` scan and a `~` scan are the same tree.
`gfind -xdev` can't do this: firmlinked dirs report the Data volume's `st_dev`, so it would prune
`/Users`.

**Plan:**
1. ✅ `index`/`capture` `-o/--one-fs` (`0f8558c`): the walker checks each dir's mount status;
   gfind prunes the mount points below the root (from `mount`), which is equivalent, and now
   prints pruned dirs as empty leaves like the walker does. gfind and walker frames are identical
   on `/Volumes` and `/System/Volumes`. Remaining: `laptop-scan` captures `/` instead of `~`
   (≈+17% entries; `m3`'s call, after Phase 5's routing).
2. **Volume rows:** ✅ capture side. `disk-tree volumes [PATH]` (`d48c637`) and
   `_SUCCESS.json.container` (`3e71190`) record the APFS container (volumes' `CapacityInUse`,
   mount points, snapshots, free). Remaining (site/ingest): carry it to the snapshot and show the
   non-Data volumes and free space as top-level cells, so the map's root is the *container* and
   the numbers add up to the disk.
3. **Residual:** `Data CapacityInUse − Σprivate(walked Data files)` bounds what the walk couldn't
   see (root-only dirs, snapshots' private blocks). Needs `--private` in the capture (+55% walk
   time), or accept the apparent-size version with the clone caveat.
4. **Root-only dirs** (the 285): a privileged helper (`SMAppService.daemon`, a bundled
   LaunchDaemon) could walk them. Open: whether `SMAppService` accepts a self-signed (no Team ID)
   bundle for daemons; if not, `sudo` scans (which lose the TCC identity) are the fallback, and
   the residual is honest enough.

### Scope policy (Ryan, 2026-09-30: "users should be able to give the app whatever perms are
needed to scan the full disk, or just their homedir")

Permissions and scope are separate axes; the default is **the whole machine, with whatever the
user can read**:

| user | what `capture -o /` sees | unseen (shown as a residual cell) |
|---|---|---|
| admin + FDA granted to disky | everything except root-only dirs | ~285 root-only dirs (`/private/var/db`, …) |
| any user, no FDA | the above minus TCC-protected trees (`~/Library/{Mail,Messages,Safari,Containers…}`, `~/.Trash`, other users' homes) | those too (69 GiB on this Mac) |
| any user, scope "home" | `~` only (opt-in, for privacy) | everything else |

- Without FDA, a walk of `/` reads `/Applications`, `/Library`, `/opt/homebrew`, `/usr/local`,
  `/Users/Shared`, most of `/System` and `/private/var` (all world-readable); the TCC trees fail
  as permission errors, they don't prompt. `~/Desktop`, `~/Documents`, `~/Downloads` and
  removable volumes *do* prompt once per app identity — one more reason the walk runs as the app.
- The app knows which row it's in: `disky probe` (FDA) and the volume table (residual size). The
  menu bar shows it and links to the FDA pane (`x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles`).
- Root-only dirs need a privileged helper (`SMAppService.daemon`); open whether that accepts a
  self-signed bundle. Until then they're the residual, which `disk-tree volumes` bounds.
- A `/` capture reduces cleanly (checked 2026-09-30 with a synthetic `/`-rooted capture: scan path
  `/`, rows `Users/…`, `Applications/…`); capture dir slug is `root`. Ingest + site handling of a
  `/` root is `m3`'s.

## Naming

The app is **disky** (`productName`/`mainBinaryName`, 2026-09-30), matching the repo and the
site; the bundle id stays `com.runsascoded.disk-tree`, so the FDA grant carried over (verified:
`agentctl check` passes for `~/Applications/disky.app` with the grant made for `disk-tree.app`).
Changing the bundle id would need a re-grant; the CLI/package stay `disk-tree`.

## Menu bar (Phase 7) — first cut ✅ 2026-09-30

`disky` launches as a menu-bar item (`ActivationPolicy::Accessory`, no Dock icon); closing the
window leaves it running. The menu (verified via System Events: items and enabled states):

- **"Scanned 3h ago · next 18:00"** / "Scanning…" / "Last scan failed (exit 1) …": the scan
  agent's plist (`StartCalendarInterval`, `StandardOutPath` mtime) + `launchctl print` (running,
  last exit). No state of disky's own; refreshed every 20 s (`status.rs`, unit-tested).
- **"Full Disk Access ✓"**, or an enabled "Grant Full Disk Access…" that opens the FDA pane
  (the in-process probe, so it's the app's own grant).
- Scan now (`launchctl kickstart`), Open disky (window), Open in browser, Show logs, Quit.
- The window loads `DISKY_URL` (default https://disk.rbw.sh). The Flask backend spawn and the
  `native_walk_stats` command are gone (routed agents already walk as the app, via the bundled
  `dt-walker`); `ui/` + Flask stay available outside the app.

**Agents + login item (2026-09-30, revised 2026-10-01).** The agent definitions live in the bundle
(`Contents/Library/LaunchAgents/com.runsascoded.disky.{scan,drain}.plist`), but `disky agents
register` installs them as **plain per-user LaunchAgents**: it renders each template into
`~/Library/LaunchAgents/` (`BundleProgram` → this executable's absolute path) and `launchctl
bootstrap`s it. Login Items still lists them as "disky", because BTM attributes them by
executable path.

- **Why not `SMAppService.agent`** (the first cut): for a bundle with no Team ID (self-signed),
  launchd pins a registered agent to the build that registered it. After a rebuild it SIGKILLs the
  agent at spawn ("Code Signature Invalid", Launch Constraint Violation, exit 78). Re-registering
  cleared it once, but not after the next rebuild, and the drainer crash-looped until
  `agentctl unadopt` restored the hand-written plists. Plain plists exec by path and survived
  every rebuild: verified with a real new build (0.1.0 → 0.1.1, new CDHash), with both agents
  spawning and no crash reports. Revisit `SMAppService` once disky has a Developer ID (Team ID).
- The login item stays `SMAppService.mainApp` (LaunchServices opens the app; no launchd
  constraint).
- Per-user command, env and log name: `~/.config/disk-tree/disky.json` `jobs`. `disky job NAME`
  redirects the job's output to `~/Library/Logs/disk-tree/<log>.{out,err}.log`.
- `agentctl adopt` moved this Mac's hand-written plists into `disky.json` and the app's agents;
  `unadopt` reverts. `agentctl install` re-renders the installed plists from the new bundle.

## Settings (2026-10-01)

`disky.json` `settings`: `site` (`prod` | `dev` | a URL; `DISKY_URL` overrides), `scope`
(`machine` → `DISKY_SCAN_ROOT=/`, `home` → `$HOME`; m3's `laptop-scan` reads it and passes `-o`
for `/`), `schedule` (local `HH:MM` list).

- **Schedule as a setting:** the scan agent wakes every 15 min (`StartInterval 900`) and runs
  `disky job scan --scheduled`, which starts the scan only if a slot passed since the last start
  (run state in `disky-state.json`: last start/end/exit, a force flag). One catch-up run after
  sleep. The first wake after install records a baseline (no surprise scan). "Scan now" sets the
  force flag and kickstarts. Unit-tested (`settings.rs`).
- **Settings window** (local `apps/tauri/settings/index.html`, `frontendDist`): FDA status (polls
  every 3 s) with a link to the pane, scope, schedule presets or custom times, scheduled
  scans on/off, open at login, site. It opens on first run, or whenever FDA is missing.
  Only the `settings` window has a capability; the remote site window gets no IPC.
- **Tray:** a "Site" submenu (disk.rbw.sh / dev.disk.rbw.sh; switching re-points an open window),
  plus "Settings…". The status line reads the run state ("Scanned 3h ago · next 18:00",
  "Scanning…", "Last scan failed (exit N) …", "… · scheduled scans off").

### Sign-in inside the window ✅ app side (2026-09-30)

disk.rbw.sh is private and its sign-in is Google OAuth, which Google refuses in embedded webviews
(`disallowed_useragent`). So the person signs in in the system browser and hands the app a
one-time credential. Site half: `specs/app-link.md` on `cloud` (87ac278): `POST /api/app-link`
mints a 60 s single-use link for the caller's own email and scopes; `GET /auth/app-link?token=`
redeems it into an ordinary session cookie; the user menu's **Open in disky** navigates to
`disky://open?link=<url>`.

App half (`applink.rs`):
- `Info.plist` registers the `disky` URL scheme (`CFBundleURLTypes`; Tauri merges
  `src-tauri/Info.plist`); `RunEvent::Opened` delivers the URL, cold launch included.
- **Only** `https://<site origin>/auth/app-link?…` is loaded (stricter than the contract's "same
  origin": the redeem path only), into the main window (created or navigated). Anything else is
  refused with a `disky.log` line that never echoes the URL (it may carry a token). Unit-tested:
  http, other hosts, look-alike hosts, other ports, other paths, `javascript:`, missing `link`.
- The webview's user agent ends in ` disky/<version>`, so the site hides the button in the app.

Verified 2026-09-30: the UA (a local server logged `…(KHTML, like Gecko) disky/0.1.0`); a
wrong-origin link refused and logged; `open 'disky://open?link=https://disk.rbw.sh/auth/app-link?token=…'`
with the app quit cold-launched it into one window on that URL. **Redemption verified
2026-10-01 on dev.disk.rbw.sh** (`DISKY_URL=https://dev.disk.rbw.sh`): D1 `access_log` shows
`mint` (Chrome UA) → `redeem` → `revoke` → `signin` (`disky/0.1.0` UA) in the same second, and the
window rendered the signed-in map. A first attempt redeemed a link Chrome had held behind its
"Open disky?" prompt for 34 min: `deny`/`expired`, as designed. prod disk.rbw.sh gets the route at
`m3`'s Phase 4 cut-over (it still serves `ui/`).
Open: a download URL for the site's "install the app" hint (none until disky is distributed).

## Remaining work (v2 not yet "real")

1. ~~**PyInstaller sidecar**~~ — dropped for now: the window loads disk.rbw.sh, and the agents
   run the project venv's CLI. Revisit if disky ships to machines without a checkout (then the
   sidecar, or the CLI, is what `agent` runs).
2. ~~**Ship `dt-walker` as a bundle resource**~~ ✅ 2026-09-30: `bundle.resources` +
   `beforeBuildCommand` put it at `Contents/Resources/dt-walker`; `locate_walker()` resolves it,
   and `agent` sets `DISK_TREE_WALKER` for its child when unset (verified under launchd). So once
   routed, the scheduled `capture`/`index` walk natively. Known difference from gfind: the walker
   also lists *unreadable* dirs (as 0-byte leaves) that gfind omits; sizes are identical.
3. **In-process walk → aggregation** — a Rust aggregation instead of the subprocess seam. Not
   needed for TCC (a routed agent's `dt-walker` child already reads as the app, verified);
   only a throughput/packaging lever now.
4. Real app icon (current is a placeholder).
5. Phase 5 cut-over (above), Phase 6 plan, Phase 7.
6. **Private size in the record stream** (`m3`'s `apfs-sharing.md`): `--private` currently only
   sums; emitting it needs a record-format extension + the Python parser, and costs +55% walk time.

## Open questions / risks

- `%b` bulk-vs-`st_blocks` fidelity (see above) — resolved empirically in Phase 1.
- Whether to eventually retire the sidecar and port aggregation to Rust (out of scope for v2;
  the stream seam keeps that door open).
- Scheduled scans: resolved — the LaunchAgents run *through* the app (`agent` mode, Phase 5).
- Branch naming: Ryan is floating `app` → "macos" (packaging, scheduling, permissions) and `m3` →
  "local" (FS semantics). Undecided.
