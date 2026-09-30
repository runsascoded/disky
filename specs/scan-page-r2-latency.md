# `/api/scan` takes 17–23 s when the scan's blobs live only in R2

From the `~/.disk` cleanup session, 2026-09-26. User: "even that long on 1st load is unacceptable".

## Repro

Scan 133 (`/Users/ryan`, written with `index --to r2://disk-tree/scans`) has a 12 MiB root blob plus 4 chunk blobs: `c` 131 MiB / 3.6M rows, `Library` 116 MiB / 1.4M, `.cache` 30 MiB, `.pyenv` 6 MiB. All were in R2 only. Server env: `DISK_TREE_SCAN_DIRS=r2://disk-tree/scans:~/.config/disk-tree/scans`.

`GET /api/scan?uri=/Users/ryan&depth=2&max_rows=2000` took **16.8–23 s on every request**, including repeats. Nothing is cached across requests.

## Profile (cProfile via the Flask test client)

17.1 s total, of which 15.4 s is `blobfs.read_parquet` → s3fs `_fetch_range`.

The main cost is the loop at `server.py` ~815–835: for each direct child with a `child_scan_id`, it runs `blobfs.read_parquet(resolve_blob(child_scan))`. That reads the **whole chunk** (all rows, all columns), then filters `depth == 1` in pandas, just to get ~100 rows per chunk.

Timings per chunk on this laptop's link, which downloads from R2 at about 19 MiB/s:

| chunk | rows | full read, R2 | `depth==1` filter, R2 | full read, local | `depth==1` filter, local | `depth==1` + 3 columns, local |
|---|---|---|---|---|---|---|
| `c` | 3.6M | 6.5 s | 9.6 s | 0.71 s | 0.17 s | 0.08 s |
| `Library` | 1.4M | 5.7 s | 6.2 s | | | |

The pushdown barely helps remotely: blobs are written with **1,048,576-row row groups**, and the first group spans depth 0–9, so `depth == 1` still fetches about 35 MiB.

## Mitigation applied (by us, not a fix)

I copied the 5 blobs (296 MiB) into `~/.config/disk-tree/scans/`. The search path prefers the local dir, so the same request now takes **0.48–0.69 s**. `/Users/ryan/c` takes 0.15 s and `Library` 0.24 s.

Scans written `--to r2://` stay slow until someone does this by hand, and doing it defeats the "zero local disk" point of `--to r2`.

## Asks

1. **Child-chunk reads in `get_scan`:** pass `filters=[('depth', '==', 1)]` and only the needed `columns`. Also cache them: blobs are immutable (UUID-named), so a per-process LRU keyed by blob ref is always valid. This is a cheap, big win locally, and for the second and later loads remotely.
2. **Smaller row groups** in `write_parquet`/`write_table` for scan blobs (e.g. 64K rows), so a `depth <= 2` pushdown over R2 fetches kilobytes, not a 35 MiB group. The files are sorted by `(depth, path)` already, so this is just the row-group size.
3. **The first load has to be fast too.** Two options, your call:
   - Denormalize each scan's shallow levels (e.g. depth ≤ 2 rows, or at least each chunk's depth-1 rows) into a small sidecar or SQLite at index time, the way `.groups.json` is written for `--to`. Then a page load never touches the big blobs.
   - Keep a local read-through cache of remote blobs, bounded, in a configurable dir. This is weaker on a near-full disk; the sidecar is better.
4. `load_scan_data` (hybrid) was another ~1.5 s of the 17. Check it after 1–3.

Target: `/api/scan` for the top of an R2-only `~` scan in under 1 s on the first request.

## Direction from the user: serve views from the cloud, never download scans to the laptop

User, 2026-09-26: "we should be able to stream views from the R2 storage much faster, may as well use cloud infra to serve an API BE even about local [scans]… def don't want to have to DL big scan data to local".

So the local blob copy above is a stopgap only, to be removed once this lands. What that means concretely:

- **Views of R2-hosted scans come from the edge API.** The Pages project `disk-tree` already binds the `disk-tree` bucket (`SCANS`, prefix `scans/`), where laptop scans land, and `ui/cfn/scanRead.ts` already does pruned reads with chunk resolution. What's missing is a way for the laptop UI (`m3:7791`) to use it: the Flask `/api/scan` should proxy to the edge, or the FE should call the edge directly for R2 scans. Either way, the view path should never pull a blob through the laptop.
- **The local server keeps local actions only:** delete/dispatch, reveal in Finder, anything needing the laptop filesystem. That matches the existing "Flask peer only" split for staged deletes.
- **The edge alone won't be fast.** A Worker can't cheaply decode a 35 MiB, 1M-row row group per request (128 MB memory limit, CPU time). Asks 2 and 3 above (small row groups, a shallow-levels sidecar or D1 rows written at index time) are what make an edge read a few KB. Without them, moving the read to the edge only moves the slowness.
- `disk-tree.pages.dev` returns `401` unauthenticated to curl. I haven't measured edge latency for scan 133; please measure it before and after.

## Scan page table on phone: path column unreadable

Screenshot, `m3:7791/file/Users/ryan` at about 390px wide: the table's columns are checkbox, kind icon, **P**(ath), Size, Modified, Children, D(esc). The path column gets about 1 character (`c`, `L`, `D`, `.`…), while Size, Modified, Children and Desc keep their full width and Desc still overflows off-screen. On a phone the name is the most important column.

Ask: apply the Staged-page treatment here too. Use `table-layout: fixed`, give the path column the remaining width with middle-elision and a tooltip, keep Size, and move Modified, Children and Desc behind a breakpoint or toggle on narrow viewports. No horizontal scroll at 390px.

## Landed (2026-09-26, `cloud` WT): asks 1–4 + the phone table

Measured on scan 133 with its blobs local (the stopgap copies), `GET /api/scan?uri=/Users/ryan&depth=2&max_rows=2000` through the Flask test client:

| state | cold | warm |
|---|---|---|
| before (whole-chunk reads, no cache) | 0.48–0.69 s (their timing) | same |
| filtered + projected chunk reads, per-process cache | 0.32 s | 0.09 s |
| `.shallow.parquet` sidecar (16 KB, 207 rows) | **0.10 s** | 0.07 s |

- **Ask 2 — root cause of the 1M-row groups:** `HybridBackend._save_parquet_arrow` (the path every `index` save takes, root and chunks) called `blobfs.write_table` without a `row_group_size`, so pyarrow's 1Mi default applied; `_save_parquet` (delete rewrites) already passed `BLOB_ROW_GROUP_SIZE`. Now both write 64K-row groups (`test_row_groups_are_bounded`). Existing blobs keep their layout until rewritten (`migrate-row-groups`, local-only).
**R2-only, measured by the `.disk` session (2026-09-26).** I moved the 5 local copies and the local sidecar aside to `~/.disk/tmp/scan133-local/`, then ran `disk-tree shallow -s 133` against R2: 15.9 s wall, which wrote `r2://disk-tree/scans/d3b58cf9-….shallow.parquet`. After that I restarted Flask on :5791 off the new WT and timed `curl` through Vite on :7791 (`depth=2&max_rows=2000`), in this order:

| uri | first request | repeat |
|---|---|---|
| `/Users/ryan` | **2.66 s** (was 16.8–23 s) | 0.33 s |
| `/Users/ryan/c` (chunk root, 1 M-row groups) | 5.02 s | 0.10 s |
| `/Users/ryan/Library` (chunk root) | 6.49 s | |
| `/Users/ryan/.cache` (chunk root) | 2.13 s | |
| `/Users/ryan/c/oa` (inside the `c` chunk) | **13.07 s** | |

The root is now fine. Drilling into a chunk on an old-layout blob is still 2–13 s on the first request, because it has to fetch a ~35 MiB, 1 M-row group over R2. The `c/oa` case is the slow one. That should go away once the scan is rewritten (64K groups) or replaced by a new one. It's worth confirming that the next `--to r2://` scan gets under 1 s on the first request at depth ≥ 2, not just at the root.

- **Ask 1 — chunk tops:** `get_scan`'s loop now calls `shallow.chunk_top_rows(root, chunk_ref, resolve_blob, columns)`: sidecar if present, else `read_parquet(chunk, filters=[('depth','==',1)], columns=<the columns the page emits ∩ the blob's>)`, cached per process in an LRU keyed on the blob's `(path, mtime, size)` (`blobfs.stat`, one stat/HEAD) — blobs are immutable except for a delete's in-place rewrite, which changes the key.
- **Ask 3 — first load:** `HybridBackend.save` writes `<root-stem>.shallow.parquet` beside the root blob (local or URL): every chunk's depth-1 rows in chunk-local coordinates + a `chunk_ref` column. Refreshed after an in-place delete that touched a chunk, removed with the scan, kept with its blob by `scans move`, excluded from blob listings (`blobfs.SIDECAR_SUFFIXES`). Backfill for existing scans: `disk-tree shallow URI | -s ID | -a` (`disk-tree shallow -s 133` was run; the sidecar sits beside the *local* copy, since `resolve_blob` prefers local — build it against R2 once the copies go). With the sidecar, a root page load on an R2-only scan fetches the root blob's `depth ≤ 2` groups + 16 KB; for scan 133's root that is still its single 379K-row group (~12 MiB, ~0.6 s at 19 MiB/s) until the blob is rewritten — new scans are fine.
- **Ask 4:** `load_scan_data` is the root-blob read; the row-group fix covers it for new scans. Not re-measured over R2 (this session has no R2 env by design; see the message to the `.disk` session).
- **Phone table:** `useNarrow(tableRef)` (container width < 600px, ResizeObserver on the table's wrapper — measured, not a media query, so a narrow pane behaves like a phone) adds `.narrow` to `.scan-details-table`: Modified / Children / Desc / Scanned (`.col-wide`) hide, names middle-elide (`elideMiddle(name, 26, 9)`, extension kept) with the full name in a tooltip (hover / long-press). Verified in Chrome with the container at 390px: table width == container, no page overflow.

### Drill-down on old-layout chunks (their R2 timings: `c` 5.0 s, `oa` 13.1 s first request)

Checked against scan 133's own `c` chunk (3.6M rows) from the moved-aside copy, rewritten into 64K-row groups (1.7 s locally): which row groups each view's pushdown keeps (min/max stats, as pyarrow prunes) and their compressed bytes at the laptop's 19 MiB/s:

| view (filters `hybrid.load` pushes down) | old: 1Mi-row groups | new: 64K-row groups |
|---|---|---|
| `/Users/ryan/c` (chunk root, `depth ≤ 2`) | 1/4 groups, 1.05M rows, 37.7 MiB ≈ 2 s | 1/56 groups, 65K rows, 2.2 MiB ≈ 0.11 s |
| `/Users/ryan/c/oa` (`depth ≤ 3` ∧ prefix `oa`) | same group, 37.7 MiB | 1/56, 2.2 MiB |
| `/Users/ryan/c/oa/marin` (`depth ≤ 4` ∧ prefix) | same group, 37.7 MiB | 1/56, 2.2 MiB |
| chunk top, sidecar fallback (`depth == 1`) | 37.7 MiB | 2.2 MiB |
| root blob, `depth ≤ 2` | 1/1, 379K rows, 12.5 MiB ≈ 0.66 s | 1/6, 1.9 MiB ≈ 0.10 s |

So a scan written by the fixed writer (any `index`, `--to r2://` included) answers every depth ≥ 2 view from one 2 MiB group: under 1 s on the first request with margin (their measured 5 s for a 37.7 MiB group includes decoding 1M rows and s3fs's block fetches, both of which shrink with the group). `migrate-row-groups DIR|URL` rewrites existing blobs in place, remote included (streaming, tmp + move; a `.groups.json` beside the blob is regenerated for the edge reader), so scan 133 gets the same without a rescan: `disk-tree migrate-row-groups r2://disk-tree/scans` from a shell with the R2 env.

## Open: views served from the edge (the user's direction)

Not built yet — it needs one decision. `disk-tree.pages.dev` answers `401` unauthenticated (Access in front), so either the Flask peer proxies `/api/scan` to the edge with a service token in its env, or the FE calls the edge directly and the user's browser carries the Access cookie. Recommendation: **FE-direct** for scans whose blob resolves to a URL (`/api/scans` can flag them; the SPA already talks to the same Functions on the static deployment), keeping Flask for local-only actions (delete/dispatch, reveal). The prerequisites the edge needs are the two above (64K groups, sidecar) — both land at index time now; the edge reader (`ui/cfn/scanRead.ts`) would read the sidecar the same way. Measure edge latency for scan 133 before/after once a token or cookie path is chosen.
