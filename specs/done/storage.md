# Scan Storage Architecture

Today disk-tree has four storage backends (`parquet`, `sqlite`, `duckdb`, `hybrid`) with overlapping but inconsistent semantics. This spec formalizes the design, names it in industry terminology, and proposes a path forward with BYO-DB support for disk-constrained users.

## Status quo

`src/disk_tree/storage/`:

- **`parquet`** — one parquet per scan. Immutable. `supports_updates = False`.
- **`sqlite`** — rows in a local SQLite DB. Mutable. Row-oriented (space-wasteful for our mostly-numeric columns).
- **`duckdb`** — rows in a local DuckDB DB. Mutable. Columnar.
- **`hybrid`** — one "root" parquet per scan; depth-1 subtrees with `n_desc ≥ 100K` get their own parquet, referenced by `child_scan_id`. Deletes rewrite only the affected chunk. `supports_updates = True`.

Metadata (scan list, denormalized stats) lives in SQLite (`Scan` table) regardless of blob backend.

## Industry terminology

What we call "hybrid" already has a name. Related concepts from the data-lake / OLTP worlds:

- **LSM tree** (Log-Structured Merge): sorted runs + memtable, writes append, reads merge across levels, background compaction rewrites. RocksDB / LevelDB.
- **Merge-on-Read (MoR)** vs **Copy-on-Write (COW)**: Apache Iceberg / Hudi table-format terms.
  - **COW**: rewrite the file on every change. Fast reads, expensive writes.
  - **MoR**: write a small delta (delete file or diff) alongside the base. Fast writes, reads merge on access.
- **Deletion vectors / position deletes** (Iceberg V2): bitmap/positions marking deleted rows in an immutable data file. Avoids rewriting the base file.
- **Tombstones**: marker records indicating a deletion (KV stores, Cassandra).
- **Compaction / vacuum**: background process that merges deltas + bases, reclaims space, may reorder/sort.
- **WAL** (write-ahead log): a pre-commit durability log, *not* the same as MoR deltas. Our current hybrid doesn't have a true WAL — it rewrites the chunk in-place on delete.

Our `hybrid` is *closest to* MoR-with-chunked-base, but without deletion vectors — we rewrite the affected chunk parquet on every delete. That's actually closer to **chunked COW**.

## Robustness concerns with the current `hybrid`

Reading `src/disk_tree/storage/hybrid.py`:

1. **No atomic rewrite.** `df.to_parquet(blob_ref, index=False)` overwrites in place. If the process dies mid-write, the chunk is corrupt. Fix: write to `<path>.tmp`, `fsync`, `os.replace`.
2. **In-process cache not invalidated across processes.** `self._cache` holds DataFrames keyed by `(blob_ref, min_depth, max_depth, follow_refs)`. Another process deleting a chunk would leave this stale. Acceptable for single-process CLI+server, but documentable limitation.
3. **Chunk threshold is a fixed constant** (`CHUNK_THRESHOLD = 100_000`). No per-scan override; no rebalancing as the tree grows.
4. **`descendant_mask` uses `str.startswith(path + '/')`** — correct, but O(rows × string-length) per chunk. Could use the `parent` column for O(1) lookups.
5. **Ancestor stat updates** (`_update_ancestors`) are local to the chunk that owns the deleted node. The root scan's summary row may not reflect a deep delete correctly when chunks are nested. Needs an audit.
6. **No checksum / versioning.** If a chunk parquet is truncated or partially written, we'd silently serve corrupt rows. Add a `manifest` table or per-chunk row count stored in SQLite.
7. **Orphan chunks.** A crash between writing child and updating root leaves a child parquet with no parent ref. `disk-tree gc` should scan the `scans/` dir and reap orphans.

None of these are disqualifying; the design works, but formalizing and hardening is worth the effort before adding R2/SSH backends on top.

## Proposed design

### Two tiers

Separate concerns the current code conflates:

1. **Metadata store** — `Scan` table, `ScanProgress` table, scan list, denormalized stats. SQLAlchemy already. **BYO via `DATABASE_URL`** (postgres, mysql, sqlite, remote sqlite via [litestream]).
2. **Blob store** — the scan rows themselves. Pluggable per-scan; different scans can use different blob backends. Default: `duckdb` (single DB file, columnar, in-place updates). Fallback: `parquet-cow` (immutable parquet per scan).

### Blob backends (proposed surface)

| Backend | Storage shape | Updates | Compression | Best for |
|---------|--------------|---------|-------------|----------|
| `duckdb` | One `.duckdb` file holding a `rows` table keyed by `(scan_id, path)` | In-place SQL `DELETE` | Columnar (Snappy/Zstd, great for our int/string mix) | Default. Small footprint, transactional. |
| `parquet-cow` | One `.parquet` per scan | None (rescan to update) | Columnar | Simple, portable, archive format |
| `parquet-mor` | Base `.parquet` + `<scan-id>.deletes` (Arrow IPC or Parquet of positions) | Append-only delete file | Columnar | If we want cheap deletes without rewriting |
| `hybrid` (legacy) | Chunked parquets w/ `child_scan_id` refs | Chunk rewrite (COW at chunk granularity) | Columnar | Large scans where we want subtree locality |

Keep `sqlite` as a deprecation candidate — its only advantage was "works without extra deps," which DuckDB also satisfies (`duckdb` is pure-Python-compatible via wheels).

### Size comparison (rough, disk-constrained user perspective)

Per user's memory: 2.5GB across 304 scans. Top scan 178MB.

- **SQLite** — row format; ~2× the parquet size for mostly-string columns (path) even with Zstd
- **DuckDB** — compares well to Parquet with Zstd on this workload (columnar, RLE on repeated paths + small int columns)
- **Parquet** — current baseline
- **Hybrid** — current, same as Parquet (chunks are just more parquets)

For a 178MB top scan: rewriting the whole thing on every delete is painful; chunked helps but needs the atomicity fixes above. DuckDB does updates in O(affected rows) without rewriting the whole table — likely the right default.

### Metadata store: BYO SQLAlchemy URL

```bash
export DISK_TREE_DATABASE_URL=postgresql://user:pass@homelab/disktree
disk-tree-server
```

Default if unset: `sqlite:///~/.config/disk-tree/disk-tree.db` (today's behavior).

Tradeoffs:
- Postgres: shared across machines, no local disk cost for metadata; requires a server
- Remote SQLite via [litestream] / [rqlite]: simpler ops, no server
- Local SQLite: today's default

Blobs can't trivially go remote — they're read frequently at high bandwidth. Propose: metadata can go anywhere; blobs stay on a configurable local path (`DISK_TREE_SCANS_DIR`, already partially implied by `SCANS_DIR`). Advanced: blobs on a NAS mount or external drive.

### Wiring up `DISK_TREE_ROOT`

Currently declared in `config.py` but unread. Proposal:

- `DISK_TREE_ROOT` overrides `~/.config/disk-tree` entirely (DB + scans)
- `DISK_TREE_SCANS_DIR` overrides *just* the blob dir (useful: metadata on local SSD, blobs on external drive)
- `DISK_TREE_DATABASE_URL` overrides *just* the metadata DB

## Compaction / GC

Today: `disk-tree index --gc` drops scans whose paths haven't been re-scanned recently (rough; keeps latest per path).

Proposed:

- **`disk-tree compact`** — for `duckdb`: run `VACUUM`. For `parquet-mor`: merge delete files into bases. For `hybrid`: merge small chunks back into parent if they've shrunk below a threshold.
- **`disk-tree gc`** — reap orphan blob files (any blob in `SCANS_DIR` not referenced by the metadata DB).
- **Scheduled nightly** via launchd/systemd (separate spec — overnight indexing) should also run compaction.

## Migration path

1. **Audit hybrid atomicity** — add tmp+rename, per-chunk row-count in SQLite, orphan reaping (`disk-tree gc`)
2. **Add `duckdb` as the new default** for *new* scans; existing hybrid scans keep working
3. **Add `DATABASE_URL` support** for SQLAlchemy metadata; test with postgres
4. **Wire `DISK_TREE_ROOT` / `DISK_TREE_SCANS_DIR`** properly
5. **Add `parquet-mor`** if we want append-only delete semantics (not urgent; duckdb covers the use case)
6. **Deprecate `sqlite` blob backend**; `hybrid` becomes legacy (still readable, not the default)

## Open questions

- **DuckDB concurrent readers/writers**: today's single-writer model is fine (scan runs in a background thread, server reads). For multi-process (e.g. scheduled scans via cron while the server is running), we need DuckDB 1.x's improved concurrency or a file-lock retry loop.
- **Lance / LanceDB** instead of parquet-mor? Lance has built-in deletion vectors. Worth evaluating but adds a Rust dep (see [indexer.md](../indexer.md)).
- **Do we need cross-scan queries?** E.g. "what's the largest file across all my scans." Today each scan is siloed in its own parquet. A DuckDB-default design makes this one SQL query. If that's desirable, it should drive the default choice.
- **Schema evolution**: the `path`/`parent`/`uri`/`depth`/`kind`/`n_desc`/`n_children`/`mtime`/`size` schema has been churned recently. Lock it down with an explicit version column on the `Scan` table, and a loader that handles old shapes.

## Decision to make

Before implementing, user to confirm:

1. **Default blob backend after migration** — DuckDB (recommended), or keep hybrid?
2. **Metadata BYO** scope — just env-var `DATABASE_URL` (simple), or full multi-tenant config (over-engineered for now)?
3. **Compaction strategy** — manual `disk-tree compact` command first, or invest in automatic/nightly?

[litestream]: https://litestream.io
[rqlite]: https://rqlite.io
