# dt-walker

Native macOS directory walker built on `getattrlistbulk(2)`, emitting the exact
`gfind -printf '%y %b %T@ %p\0'` byte stream that disk-tree's Python indexer
(`src/disk_tree/backends/gfind.py`) parses. A drop-in replacement for the `gfind`
subprocess. See `specs/tauri-native-app.md` (Phase 1).

## Why

- **TCC identity**: compiled into the signed Tauri app binary, the walk *is* the app — no
  child process — so Full Disk Access attributes unambiguously to "disk-tree".
- **No `findutils` dependency**: removes the `brew install findutils` runtime requirement.
- **Throughput**: one `getattrlistbulk` syscall returns a whole directory's entries *with*
  their attributes, above the per-file path-resolution cost that caps `gfind` (fts + lstat).

## Build & run

```bash
cargo build --release              # from apps/tauri/
./target/release/dt-walker <root>  # emits %y %b %T@ %p\0 to stdout
./target/release/dt-walker --stats <root>   # + "N records, M errors" on stderr
```

Flags: `--exclude PATH` (repeatable), `--no-default-excludes` (drop the built-in
`~/Library/CloudStorage` + `/Library/CloudStorage` prune), `--stats`.

## Stream format

Null-terminated records, single-space fields, `%p` last (so paths with spaces survive
`split(' ', 3)` on the Python side). Paths are raw bytes — macOS paths aren't guaranteed
UTF-8, and the parser decodes with `errors='replace'`.

| field | meaning |
|-------|---------|
| `%y`  | type: `f` file, `d` dir, `l` symlink, `b`/`c`/`s`/`p` device/socket/fifo, `?` other |
| `%b`  | 512-byte blocks allocated (`ATTR_FILE_ALLOCSIZE / 512`; 0 for dirs/symlinks) |
| `%T@` | mtime, integer epoch seconds |
| `%p`  | absolute path (raw bytes) |

## Parity with gfind

`parity.py` runs both walkers over a root, times each, and diffs the record sets modulo
ordering (`%T@` compared as `int(float(...))`, matching the Python parser):

```bash
./parity.py ~/some/large/subtree
```

Measured (warm cache, macOS 26 / arm64 / APFS): **byte-exact parity, 0 mismatches on ~1.06M
files**; **1.58x** faster on `~/Library/Caches` (580K files), **2.73x** on `~/c/oa/marin`
(480K files). `%b` from `ATTR_FILE_ALLOCSIZE/512` matches gfind's `st_blocks` exactly — no
resource-fork / compressed-file divergence observed.

## Notes

- macOS-only (`getattrlistbulk` is a Darwin syscall). The crate builds only on macOS targets.
- Symlinks are reported (`l`) but not followed — matching `gfind`'s default.
- Unreadable directories are counted (`EACCES`/`EPERM` → a gfind-format `Permission denied`
  line on stderr) and skipped, not fatal.
