# Serverless tier reads: `.groups.json` footer + coarse tiers (CP from mgu, no D1)

Port mgu's serverless read-layer advances into dt so the demo's Pages Functions
read fewer row groups and **avoid parsing the ~5 MB thrift parquet footer on a
cold Worker isolate**. Two separable wins, both from mgu (`serverless-reference.md`
"read layer"); dt takes them via the shared blob sidecar, **not** mgu's Cloudflare
D1 (`.groups.json` is a full substitute — confirmed against `_lib/index.ts`).

## Already landed (CP'd 2026-09-07, `mgu-engine-audit-2026-09-07.md` §4)

- `src/disk_tree/find/groups.py` — the `.groups.json` writer (schema + per-row-group
  stats/offsets + the stripped `rg_json` a hyparquet reader revives; ~250 B/group),
  byte-identical to mgu's `index_footer.py`.
- `src/disk_tree/find/tiers.py` — coarse-tier emission (`write_tiers`: fine/dirs/objects
  + a `coarse` tier at floor `F = 2^(round(log2 total) − E)`, floor in parquet KV
  metadata), byte-identical to mgu's.
- `src/disk_tree/cli/import_listing.py` — `import --to` already emits tiers + groups
  (`-i/--tiers`, `-E/--coarse-exp`, `-G/--groups`).

The open item (audit §4 #7) is the **read side**: `ui/cfn/parquet.ts` consuming
`.groups.json` and picking tiers.

## Phase 1 — emit `.groups.json` from `index --to` / `reduce --to` ✅

Only `import --to` emitted the sidecar; the demo publishes via `index --to`
(`.github/workflows/rescan-demo.yml`) and cloud runs via `reduce --to`. Added
`find/groups.write_groups_sidecar(blob)` (warn-and-continue: the sidecar is a pure
optimization — a blob without it still reads via its own footer, so a failure
never aborts a publish whose blob + manifest are already valid) and call it for
every **remote** blob beside the manifest write, in `cli/index.py` and
`cli/capture.py` (`reduce`). Local blobs skip it (the Flask server parses footers
fine on disk; the sidecar only matters for the serverless R2 reader).
`tests/test_index_remote.py::test_to_writes_a_groups_json_footer_sidecar` +
the `reduce` manifest test cover it.

## Phase 2 — read side: `ui/cfn` consumes `.groups.json` (next)

- `ui/cfn/groups.ts` (new): `loadGroups(store, blobKey)` fetches `<blob>.groups.json`
  (null on 404); `reviveMeta(doc)` reconstructs a hyparquet `FileMetaData` from the
  stripped `rg_json` + schema (port `reviveRowGroup`/`readGroup` from mgu
  `_lib/index.ts:248-271`, typed to dt's `BASE_COLS`); `selectGroups(doc, query)` =
  `parquet.ts` `selectRuns`'s predicate over `doc.groups` (`d_min/d_max/p_min/p_max`
  map 1:1 onto the row-group stats `columnStats` extracts today).
- `ui/cfn/parquet.ts`: `readRows`/`readChunkPointers` take an optional groups doc;
  when present, build metadata from it instead of `parquetMetadataAsync` (the
  cold-footer parse at `parquet.ts:111,149`). **Fallback unchanged** when absent.
- `ui/cfn/scanRead.ts`: attempt `loadGroups` per blob key, thread the doc down.
- Discovery: **probe-based** — the reader derives `<blob>.groups.json` (strip
  `.parquet`, add `.groups.json`, per `groups.py:groups_path`) and 404-tolerates a
  miss. No manifest-schema change in this cut (a `groups:true` manifest flag to
  skip the probe is a later optimization).
- Fixtures: `ui/cfn/tests/fixtures/gen.py` also emits `fixture.parquet`'s
  `.groups.json` (call `disk_tree.find.groups.write_groups`), so tests exercise the
  groups-served path; keep one fixture **without** a sidecar to lock the fallback.
  New TS test: revived `FileMetaData` reads byte-identical rows to a real footer parse.

## Phase 3 — coarse tiers + tier-select (later)

Emit coarse tiers from `index --to`/`reduce --to` (wire `TierOpts` into the emit,
as `import` already does; multi-floor needs a `.coarse<E>` naming change in
`find/tiers.py` — today one `.coarse.parquet`). Read side: `ui/cfn/tiers.ts` ports
mgu `view.ts:373-377`'s pick loop (coarsest tier whose `floor_bytes ≤ threshold`,
else fine/main blob), needing an API-shape decision (a `min_bytes`/`w`,`h` threshold
param on `/api/scan`, or a `/api/view`). Orthogonal to Phase 2's footer-avoidance.

## Phase 4 — chunk-blob sidecars (later)

A hybrid `index` blob chunks depth-1 dirs (`storage/hybrid.py`); Phase 1 emits the
sidecar for the **top** blob only (the root/overview read). Deep drills into chunk
`<uuid>.parquet` blobs still footer-parse until each chunk gets its own
`.groups.json` — emit them by walking the top blob's `child_scan_id` pointers.

## Omitted: Cloudflare D1

mgu also caches row-group footers in D1 (`index_row_groups`) as a latency tier;
its `.groups.json` blob path is the authoritative fallback and does the same
pruning over the JSON array. dt uses the sidecar **only** — no `env.DB` binding,
none of mgu's generation-pointer / gc-retire-compact machinery. Revisit only if
cold-blob footer fetches are measured to hurt at dt's scale.
