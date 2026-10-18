# Shared blocks: hatch the part of a rect that deleting it alone wouldn't free

**Status:** 2026-10-18, idea / design. Not high priority (Ryan). Local deployments only (m3, the app): object stores have no block sharing.

## Goal

A treemap rect's area is its bytes. Some of those bytes are shared with files outside it: APFS clones, Linux reflinks, hardlinks. Deleting the rect alone wouldn't free them. Draw that share as a cross-hatched band inside the rect, and put the split in its tooltip ("12.4 GB · 3.1 GB shared: not freed if deleted alone").

## What the OS gives us

| | per file | cost |
|---|---|---|
| **APFS clones** | `ATTR_CMNEXT_PRIVATESIZE`: bytes no other file shares. `dt-walker --private` already fetches it in the same `getattrlistbulk` call, but only adds Σprivate to `--stats`; the record stream doesn't carry it. With `VOL_CAP_FMT_CLONE_MAPPING`, `ATTR_CMNEXT_CLONEID` + `ATTR_CMNEXT_CLONE_REFCNT` identify a file's clone family | same syscall; measure the overhead (`--stats` both ways) |
| **hardlinks** | `ATTR_FILE_LINKCOUNT` (macOS), `st_nlink` + `st_ino` (Linux, `gfind %n %i`) | free |
| **Linux reflinks** | `FIEMAP` extents flagged `FIEMAP_EXTENT_SHARED` | one ioctl per file with `nlink == 1` and a CoW filesystem (btrfs, XFS); skip elsewhere |

`shared = size − private` per file; a hardlink with `nlink > 1` is all shared.

## Two problems, one of them a correctness fix

1. **Hardlinks are counted once per link today** (a dir holding 3 links to a 1 GB file totals 3 GB). Count each inode once, at its first path in walk order; later links get size 0 and a `link_of` flag. This is a bug fix independent of the hatch, and it makes the root total match `df`.
2. **Directory-level "freed if deleted" isn't Σ per-file private.** A dir holding *both* members of a clone pair frees all of it. Exact closure:
   - hardlinks: tractable. A subtree frees an inode's bytes iff it holds all `nlink` links (count links per inode per subtree during aggregation).
   - clones: approximate. Sharing is per block, and a clone family's members can share different extents. Rule: a subtree that holds every member of a clone family (by `CLONEID` + `CLONE_REFCNT`) frees the family's bytes; otherwise, Σ private. Exact needs extent maps, which is out of scope.

## Pipeline

1. **Walker / capture:** with `--private`, the record stream gains `private` (and `ino`, `nlink`, `clone_id` when relevant). Linux `gfind` path: `%i %n`, plus FIEMAP behind a flag.
2. **Aggregation:** a `shared` sum column on the layer-2 (the `-p` pivot mechanism), with the hardlink/clone closure from §2 applied per dir.
3. **Stores:** `shared` threaded through the path store and the interval store as one more summed column (optional; absent = 0).
4. **Site:** the treemap draws a hatched sub-band of area ∝ `shared / size` (the diff view's Δ sub-rect band, with a hatch pattern in place of the fill); tooltip line; a legend chip when any rect has `shared > 0`.

## Effort

Walker + capture: small. Hardlink-once fix: small, with tests. Closure in aggregation: medium. Store columns: medium (the interval store's column set). Treemap hatch: small. Measure first: how many bytes on m3 are shared (`dt-walker --private --stats /`) decides whether this is worth more than the hardlink fix.
