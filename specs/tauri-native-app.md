# Spec: disk-tree Tauri v2 native macOS app

Status: **in progress** (2026-09-08) — greenfield Option C from `specs/macos-app.md`. This
is the reviewable plan; it's kept in sync with the code and moves to `specs/done/` only when
v2 is real (signed `.app`, native walker feeding scans end-to-end).

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
- `tauri-cli` (Phase 2 — recorded here when installed).
- Command Line Tools (pre-existing) for `codesign`.
- Node 26 / pnpm 10 (pre-existing) for the UI build.

## Phases

- **Phase 0** — this spec. ✅
- **Phase 1** — `dt-walker` crate + CLI; benchmark & parity-diff vs `gfind`. ✅
  - **Result:** byte-exact parity (0 mismatches on ~1.06M files across two trees); **1.58x**
    faster on `~/Library/Caches` (10.1s vs 16.0s, 580K files), **2.73x** on `~/c/oa/marin`
    (3.34s vs 9.10s, 480K files), both warm-cache. Harness: `crates/dt-walker/parity.py`.
- **Phase 2** — Tauri v2 shell around `ui/dist`, Python sidecar.
- **Phase 3** — `DISK_TREE_WALKER` seam in `local.py`; scan end-to-end through the native walker. ✅
  - `LocalBackend.list` swaps the source command to `dt-walker` when `DISK_TREE_WALKER` is set,
    feeding the *unchanged* `run_gfind` null-record parser; `PERMISSION_DENIED_RE` widened to
    match the walker's `dt-walker:` prefix. **Verified:** a full `disk-tree index` (parse →
    aggregate → parquet → `du`) through the walker yields a **byte-identical scan** to the
    gfind path (`tests/test_backends.py::test_dt_walker_seam_matches_gfind`, exact DataFrame
    equality; skipped when the binary isn't built).
- **Phase 4** — sign + bundle with `disk-tree-selfsigned`; FDA-grant verification steps.

## Open questions / risks

- `%b` bulk-vs-`st_blocks` fidelity (see above) — resolved empirically in Phase 1.
- Whether to eventually retire the sidecar and port aggregation to Rust (out of scope for v2;
  the stream seam keeps that door open).
- Scheduled scans: does the LaunchAgent invoke the app (app identity on cron) or keep the CLI?
  Inherited from `macos-app.md`; not decided here.
