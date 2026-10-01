# APFS sharing: index and surface what a delete actually frees

**Status:** brainstorm + first experiment (2026-09-30). Branch `m3`; the walker half overlaps `app` (`tauri-native-app`, `dt-walker`).

## The problem

Every size disk-tree shows is **allocated blocks per path**. On APFS, clones (`clonefile`, `cp -c`) and hardlinks let many paths share one set of blocks, and each path is charged in full. So a subtree's size is an **upper bound** on what deleting it frees, and it's often badly wrong for the dirs people most want to delete:

- **`.venv`s**: uv's default macOS link mode is `clone` (not hardlinks; on Linux its default *is* hardlink). A venv's files are clones of `~/.cache/uv` entries. Measured: m3's own `.venv` is 317 MiB apparent, **23 MiB exclusive (93% shared)**; `site-packages/pyarrow` is 108 MiB, **0 bytes exclusive**. 35 dormant venvs reported 16.9 GiB and freed 9 GiB (2026-08-29).
- **`node_modules`**: pnpm's `package-import-method: auto` clones from its store on APFS.
- **Hardlinks** exist but matter little on this Mac (a 2026-08-29 census of `$HOME` found `nlink > 1` over-counting 4.3 of 385.9 GiB, 1.1%).

The CLI already answers this on demand (`overcount`: per-file `ATTR_CMNEXT_PRIVATESIZE`; `reclaim`: exact extent intersection), but nothing reaches the index, the map, or a staged delete.

## Experiment 1: private size comes free with the bulk walk

`tmp/bulkpriv/bulkpriv.c`: `getattrlistbulk` asking for `ATTR_FILE_ALLOCSIZE` plus `ATTR_CMNEXT_PRIVATESIZE` (in `forkattr`, with `FSOPT_ATTR_CMN_EXTENDED`), which is the call `dt-walker` already makes minus one attribute.

| dir | files | alloc | Σ private | vs per-file `getattrlist` |
|---|---|---|---|---|
| `.venv/…/site-packages/pyarrow` | 115 | 113.3 MB | 0 | identical |
| `~/Downloads` | 1,173 | 585.7 MB | 583.7 MB | — |
| fresh `cp -c` pair (3 MB) | 2 | 6.0 MB | **0** | — |
| + a hardlink of one | 3 | 9.0 MB | **0** | — |

So: **one extra 8-byte attribute per file, no extra syscalls** — but not free in time: APFS computes it per file. Measured by `app` (`dt-walker --private`, 2026-09-30) over `~`, 7.49M entries: walk **127 s → 199 s (+55%)**; **Σalloc 398 GiB, Σprivate 288 GiB**, so ~110 GiB of `~`'s apparent size is clone/hardlink-shared. +72 s on a twice-daily background walk is affordable; it could also run on every Nth scan, since sharing moves slowly. Emitting it per record needs a walker record-format extension plus the parser (`tauri-native-app.md` item 6).

What per-file private size means: bytes of *this file* no other file references. Consequences, visible in the last two rows:

- **Σ private over a subtree is a lower bound** on what deleting it frees. It misses blocks shared only *within* the subtree (both halves of a clone pair, or a venv deleted together with the uv cache it came from).
- **Σ alloc is the upper bound**, and double-counts hardlinks unless deduped by inode (`ATTR_CMN_FILEID` + `ATTR_FILE_LINKCOUNT`, also bulk attributes).
- The exact number for a chosen set is `reclaim` (extent intersection; opens each file, ~43 s for a venv plus its uv partners). That's the right tool for a dry run, too slow for a whole-`~` index.

## Proposal: two bounds everywhere, the exact number at delete time

1. **Capture**: the native walker emits `private` beside `blocks` per file, and dedupes hardlinks by file ID. Listing shards and the path index gain a `private` column, rolled up like `size`. (`gfind` has no equivalent; the Python `index -x` reclaim sidecar is the existing slow path.)
2. **Show**: per row, `frees ≥ Σprivate` beside the size, e.g. `.venv 317 M · frees ≥ 23 M`. On the map, a "shared" hatch over the cell's non-private fraction, or a size mode where area = Σprivate. A row that's mostly shared gets a note naming where the blocks live when it's a known pairing (`.venv` → `~/.cache/uv`, `node_modules` → the pnpm store).
3. **Stage / dry run**: the laptop executor's dry run already runs locally; make it run `reclaim` over the staged set, so `/staged` shows the **exact** bytes the real run frees (and says "deleting the uv cache too would free N more" when partners are detected).
   **Built (2026-10-01):** a dry run on a schema with `deletion_runs.freed_bytes` (migrations cw `0008` / gcs `0032`) runs `extents.measure` over the staged set as a whole (`drain.execute_run(reclaim_fn=…)`, wired in `dispatch --serve` on macOS) and records `unique`; `/staged` shows "· frees X" beside the would-delete size, with a tip on why they differ. Measured on m3's `.venv`: 891 MiB apparent → **52.8 MiB** freed (797 MiB cloned from `~/.cache/uv`), 50 s (mostly walking the 1.09M partner files). The "would free N more with the cache too" hint is not built.
4. **After a real run**: record the `df` delta beside the tool's count; the gap is the lesson.

## Other local-FS effects worth surfacing (later)

- **Transparent compression** (APFS/HFS+ `com.apple.decmpfs`): alloc < logical. `%b` already reports alloc, which is right for "space".
- **Sparse files**: `Docker.raw` is 1 TiB logical, 20.7 GiB allocated. Blocks already handle it.
- **Space outside `~`**: the container's other volumes: `VM` (swap, 11 GB now), local snapshots (e.g. a pending `MSUPrepareUpdate`), purgeable space. That's the whole-disk indexing thread (`app`).
- **Linux**: uv hardlinks by default there, which `nlink`/inode dedupe catches cheaply. Btrfs/XFS reflinks have no `PRIVATESIZE` equivalent (`FIEMAP` shared flags per extent). Wait until we're on those machines.

## Where it lives

Engine + walker changes are `[base]` (and the walker is `app`'s crate); the `laptop` store's UI is where it's first exercised. Open question: whether `m3` becomes the "local" (filesystem-semantics) branch and `app` becomes "macos" (packaging, scheduling, permissions).
