# CP manifest: marin's fork-side `src/disk_tree` → upstream

> **Landed upstream** `5be454c` (2026-09-04). `tree_build.py` (new) +
> `access/read_sizes.py` (new) + `dt access sizes` + the `aggregate`/
> `parsers/gcs`/`schema` productionization, taken from `m/gcs` (upstream was ==
> the merge-base on every file, so each is marin's additive delta only).
> `cli/__init__.py` left untouched — `access` is already imported, so `sizes`
> registers for free; marin's version there only *removes* upstream commands.
> Tests: `test_tree_build.py` + `test_read_sizes.py` (new), `test_access.py`
> refreshed for whole-record dedup. Full suite 454 pass. The "already upstream"
> items (atomic import write, `preserve_insertion_order=false`) were confirmed
> present. Marin can record `5be454c` in `dt-core-upstreaming.md`'s sync state.


Written from the marin-gcs-usage session (`gcs` branch). Goal: keep upstream's
Python core a superset of what the marin branches use, so the CP graph stays
bidirectional (marin keeps its own copy of the engine per branch — see marin's
`specs/branch-parity-discipline.md`; this is not a move to pinning). Marin
imports exactly:
`tree_build.{build_tree,DirRow}`, `access.aggregate.aggregate_access`,
`access.read_sizes.aggregate_read_sizes`, `access.parsers.{parser_for,
gcs.DEDUPE_WARN_FRACTION, gcs.dropped_fraction}`, plus the `disk-tree bulk-list`
CLI.

Fork commits to bring across (oldest first; `git log gcs ^dt/main -- src/disk_tree`):

- `3553a81` shared arbitrary-depth tree builder (`build_tree`) → `tree_build.py`
- `d2748e8` tree_build: additive-field model, `(other)` = parent − kept
- `6fd4b55` tree fold floor: parent-relative, not fleet-relative
- `97a7814` tree floor: bound the parent-relative keep set (`ABS_FLOOR` + `TOP_K`)
  (the tracker deferred these pending the pixel-budget redesign; with the
  path-index/`/api/subtree` now serving the pixel budget server-side, the
  floors are just the layer-2 rollup contract — safe to land as-is)
- `376477a` access-log ingest productionized: `(bucket,path)` keys, `last_ts`
  atime, incremental ingest, webdata `-x` join
- `47ff70e` GCS usage-log dedup: key on the whole record, not `s_request_id`
- `82cd0a2` layer-2b read-size histogram (`dt access sizes`)
- `access` follow-ups already on both marin branches: `DISTINCT ON` dedupe
  projection, atomic import write, `preserve_insertion_order=false`

Not requested: upstream's diff index / vocab sidecar / compare perf — marin's
prod has no Python server, so nothing there consumes them. The `[CP→disk-tree
upstream]`-tagged bulk-list fixes (`168308a`, `937759b`, `aee7a26`) are already
upstream.

When landed, note the upstream SHAs in `dt-core-upstreaming.md`'s sync state so the next scramble's git-didi run has a clean base.
