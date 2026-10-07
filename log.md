# Cleanup log

Running record of disk-cleanup passes on this Mac. Newest first. Each entry: what was scanned (free space before/after), what was deleted, and the **measured** bytes freed (`reclaim`/`overcount`, not apparent size).

## Deployment maintenance (2026-10-05): restore scheduled scans and the drainer

- Both agents were still loaded through stale SMAppService registrations, with launchd exit 78 / launch-constraint failures. Re-registered them using disky's plain LaunchAgent plists; the drainer's D1 heartbeat resumed. Persistence across reboot remains unverified; the app worktree has a spec for detecting and migrating legacy registrations.
- Fresh whole-machine capture `captures/m3/root/2026-10-05T15-41-33Z`: 7,213,352 files, 37 shards, 286 inaccessible paths, 4m55s. `_SUCCESS.json` triggered Batch job `729919f9-161e-497e-95fb-f8970628ec64` six seconds later. The job succeeded, publishing R2 and D1 generation `202610051541` (8,140,733 index rows). `du -p` reads it back; `~/Library` is 121 GiB, including 72.9 GiB of Application Support and 9.83 GiB of Containers, so FDA coverage is present.
- Boot-container free space at capture completion: **58.0 GiB**. No cleanup targets were deleted; measured bytes freed: **0**.
- Retired `aws/laptop-scan`; the drainer's post-delete refresh now uses `disky scan now`, so scheduled and requested scans share the in-process capture and R2 trigger instead of submitting Batch twice.
- Published the 34 existing commits through `ab8e3098` to `r/m3`; GitHub CI run `37336199262` passed. All 597 site tests passed (one skipped). Dev deployment `2b17a9e5` and production deployment `288e3c65` are live. RACx browser checks verified the signed-in map, today's scan, hidden root staging controls, admin `/staged`, and the dev phone layout. Live scratch dry-run verification remains pending explicit path-navigation approval; no staged items were added or deletion runs dispatched during these checks.
- Added a physical disk-space bar above the map: **62.25 GB / 58.0 GiB free of 494.38 GB / 460.4 GiB, 87.4% used**, measured during this capture. The ingest now copies the capture's container metrics into `meta.json.disk_space` through `disk-tree capture-meta`. Updated the Batch image to digest `597342ff…`; re-ingest job `9457fa20-b1a2-498d-bf07-4d64e6e27e13` succeeded and published those measurements, with tree totals unchanged. The UI follows SI/IEC preference, labels the reading "at scan", and explains physical vs apparent size in a pinnable tooltip. Scan freshness follows the capture timestamp, so re-ingesting does not make an old measurement look fresh. Desktop/mobile CIC passed; 599 site tests and 10 capture/metadata tests passed.

## Pass 5 (2026-10-03): candidates from the scheduled capture's path index

- Source: the 06:15 capture's path index (`listing/laptop/2026-10-03/index/202610031015`) via `disk-tree du -p`, then `overcount` on each candidate. The first pass driven by the scheduled scan rather than a hand-run one.
- Free before: 12 G (98%) at the morning's check; 24 G by the start of the deletions (the `crashes` session's own cleanup, plus Ryan's `dkpra`: `Docker.raw` 20.8 G allocated in the 09-30 scan → 7.7 G after it compacted). Swap: 22.5 G used (`VM` volume 24.7 G); a reboot is the next big win.
- Deletions (measured `df` deltas):

| target | freed |
|---|---|
| Pulumi plugins nothing pins (aws 6.83.4 + 7.20.0, cloudflare 6.14.0, 2 stale schemas; kept aws 7.44/7.48, cloudflare 6.20/6.21, which project venvs pin) | 1.73 GiB |
| JetBrains (app already uninstalled): `Application Support/JetBrains` (2.7 G of it plugins), `Logs/JetBrains`, `Preferences/*jetbrains*.plist`. Settings minus plugins backed up first: `nas:/mnt/user/backups/m3/jetbrains/intellij-settings-2026-10-03.tar` (98 MiB, 4,862 entries = local count) | 3.06 GiB |
| `~/.pyenv` (unused since 2025-09; `~/.rc` dropped pyenv). Moved to `nas:/mnt/user/backups/m3/pyenv/pyenv-2026-10-03.tar` (4.08 GiB, 203,532 entries = local count) | 4.26 GiB |

- Total **9.05 GiB**; free after: **34 G (93%)**.
- `~/c/disky/tmp` (root, on Ryan's go): 1,008 dormant entries (2.4 G apparent, ~1.85 GiB exclusive) → `/usr/bin/trash`, not yet emptied, so `df` is unchanged until the Trash is emptied; the `local` worktree moved out first (`tmp/wt-local` → `wt/local`). Free then: 43 G (91%), counting other sessions' cleanup (`crashes`).
- `go clean -cache` (`~/Library/Caches/go-build`, unused since 09-29; the cost is one slower first Go build later): **3.02 GiB** measured. Free: 46 G (91%).
- Left: `wt/app` Rust `target/` (9.4 GiB exclusive; disky-app's), dormant `oa/marin/wt/*` (2.2 GiB exclusive), Chrome/Superhuman CacheStorage. Signal untouched.
- `oa/{cubed,mamba,ops,gha-runner,plant-caduceus}` have `.python-version` files naming the archived pyenv virtualenvs; uv won't resolve those names.

## Pass 4 (2026-09-25): staged plan dispatched from the web UI

- Disk hit ~0 free (swap growth); reboot cleared swap, back to 36 G free.
- Staged plan 1 (21 items): 18 `~/Downloads` installers / zips duplicated by their extracted dirs, Claude desktop `vm_bundles/claudevm.bundle` (Cowork VM image), `~/Library/Caches/Homebrew`, `~/.cache/puppeteer`.
- Dispatched from the Staged page in three real runs, 19:29–19:30: 9.8 MB / 6 objects, 1.46 GB / 12, 16.52 GB / 10,268 — **18.0 GB (16.76 GiB) by the tool's count**.
- Free space afterwards: 43 G (2026-09-26 morning, 91%).

## Pass 3 (2026-09-24): tool-native GCs

Browsed scan 133 via a side disk-tree UI (`:7791` Vite → `:5791` API, `DISK_TREE_DELETE_APPROVAL=staged`; added the missing `deletion_run.batch_job` column by hand, backup `tmp/disk-tree.db.bak-20260923`). Measured freed bytes (`df` delta):

| GC | freed |
|---|---|
| Pulumi plugins: **all** versions removed (a sidecar-file bug in my keep-newest filter took the newest too; Pulumi re-downloads on demand) | 5.54 GiB |
| `go clean -modcache` (`~/c/go/pkg/mod`) | 3.10 GiB |
| `uv cache prune` (reported 4.3 GiB; the rest is reflink-shared with venvs) | 0.33 GiB |
| `brew cleanup -s` (the dry-run estimated 1.9 GB) | 0.19 GiB |

Free: ~8 G → 41 G (92%); the GCs account for ~9.2 G, the rest came from outside these GCs (probably swap shrinking; not verified).

Findings: the Superhuman Mail extension's CacheStorage holds **17.3 GB** across 5 Chrome profiles (NBDx 9.25 G: `renders` 4.7 G, an unnamed 3.8 G cache, `attachmentUploads` 0.9 G). Claude desktop `vm_bundles/claudevm.bundle` 11 G. Downloads: installers 1.7 G, zips whose extracted dir sits beside them ~0.56 G. Signal (21.8 G) left alone at Ryan's request.

## Pass 2 (2026-09-23): fresh `~` scan to R2; swap is the hidden consumer

- Since pass 1b: cleared stale caches (JetBrains 2.3 G, Copilot `project-context` 1.9 G, Cypress 1.2 G, `pre-commit clean` 0.76 G) = **6.18 GiB measured**; Ryan ran `dkpra` (Docker prune) = ~34 G, which refills over time. `crashes` DVC cache: `dvx gc -w -s` would free **8.81 GiB** (all verified in `s3://nj-crashes`); not yet run. `ctbk` gc frees ~0. Neither repo is deletable: `crashes` has 142 commits on no remote + 4 stashes, `ctbk` 9 + 5 stashes + UCs.
- Free before this pass: **12 G (98%)**. Scan: `disk-tree index -C -q -D --to r2://disk-tree/scans /Users/ryan` (disk-tree on `cloud` @ `47eee40`) → **scan 133**, blob `d3b58cf9…`, 6.64M files, 384 GiB, 4m33s, 0 bytes to the store. Peak RSS **7.1 G** (4.3 G on Sep 9), peak footprint 10.6 G.
- Free after: **7–8 G (99%)**: the scan's memory peak grew swap. The `VM` APFS volume holds **31.2 G** of swapfiles (28 G used). No local TM snapshots.
- `~` since Sep 9: 392 → 384 GiB (`Library/` 140 → 122, `c/` 173 → 177, `Downloads/` 6.1 → 8.1, `.npm/` new at 2.6). Home shrank, yet free space fell, so the growth is swap, outside `~`.
- The `com.runsascoded.disk-tree.index` LaunchAgent (6 h, `--require-external`) is a no-op without x6 (its log paths are on x6, so launchd reports exit 78). No boot-disk writes.

## Pass 1b (2026-09-09): dev session fixed all 3 R2 bugs; verified end-to-end

The `disk-tree` dev session implemented `specs/r2-scan-target.md` (commit `5c32460`): `fixed_upload_size=True` on the R2 s3fs, a `config.blob_reachable` helper with reachable-blob fallback in `freshest_scan_covering`/`load_or_create`, and `previous_scan`/`build_previous` skipping unreachable blobs so a diff can't fail a persisted scan. 233-test sweep green there.

Verified here on a real `~` scan via the **plain** CLI (no wrapper): `disk-tree index -C -q --to r2://disk-tree/scans /Users/ryan`. Blob `463f8626…` (**scan 124**, 6.49M rows) wrote to R2 clean — no `InvalidPart` — and the diff picked `123→124` (a reachable previous blob, not the unmounted scan 74). `du -d1` reads back off R2 fine. The run was later **OOM-killed by the OS during the diff-index build** (boot disk 98%, machine under load); the scan + DB row were already committed, so scan 124 is intact. On a tight machine, add `-D` to skip the diff. `CLAUDE.md` R2 recipe updated; `tmp/dt-r2.py` wrapper now obsolete.

## Pass 1 (2026-09-09): fresh `~` scan streamed to R2, no deletions

- Exercised `index --to r2://disk-tree/scans` for real (`AWS_PROFILE=cf` + `DISK_TREE_R2_ENDPOINT_URL`). First full run failed at the multipart commit (R2 needs `fixed_upload_size=True` in s3fs); re-ran via `tmp/dt-r2.py` (monkeypatches `blobfs._s3fs`) — **scan 123**, blob `4b9ce0c5…`, 6.49M rows, 284 MB / 5 objects in R2, **0 bytes written to boot**. Walk 5m09s, peak RSS 4.3 G. Findings handed to the dev session in `~/c/disk-tree/specs/r2-scan-target.md`.
- Boot disk: 12–15 G free during the pass (fluctuating with other processes), still 98%.
- `du -d1` (via `DISK_TREE_SCAN_DIRS=r2://disk-tree/scans`), 392 GiB total: `c/` 173 G, `Library/` 141 G, `.cache/` 38.8 G, `Downloads/` 6.1 G, `.pulumi/` 5.3 G, `.claude/` 5.2 G, `.pyenv/` 4.2 G. Since Aug 29: `c/` −11 G, `Library/` +6 G, `.cache/` +4 G.
- Next: descend `c/`, `Library/`, `.cache/` (`du -d2`), then `overcount`/`reclaim` before touching anything.

## Baseline (2026-09-09, workspace init)

- **Boot disk `/`: 461 G total, 447 G used, 14 G free — 98% full.** Tight; treat as crisis mode (prefer cloud/external scan targets over writing blobs to boot).
- **x6 external SSD: not attached** (only `Macintosh HD` mounted).
- `disk-tree` store (`~/.config/disk-tree/`): 1.2 G — blobs + a ~0.85 G `scans.duckdb`. Itself a cleanup candidate (`disk-tree scans -g` / `scans move`).
- Newest `/Users/ryan` scan in the index is ~9 days old (~413 G). **First pass: fresh scan + `du -d1`** to find current weight, then `overcount`/`reclaim` on the fat nodes before deleting anything.

_No deletions yet._
