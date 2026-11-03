# Map before search: an opt-in serving order

Status: spec only. Ryan (via gcs, 2026-10-25): an option a deployment can enable, off by default (gcs off). Low priority under the stability hold; no go to build yet.

## Why

A scan's map (path store, D1 rows) is ready once `index`, `publish-r2` and `interval-store` end. Today `job/nf/scan.nf` holds `index-sync` until the static-names append ends too:

```groovy
def serving = gathered(rows.mix(st, iv, sided))
def synced = folded('index-sync') ? serving : gathered(index_sync(serving))
```

The map's visibility therefore waits on search indexing, ~20 min on gcs today (catalog / drill / anchors runs). At that lag Ryan doesn't want this as the default. A deployment whose append runs long, or that prefers the map first, should be able to turn it on.

## Option

`options.serving.map_first` (bool, default false). Off: exactly today's order. On:

```groovy
def map = gathered(rows.mix(iv, sided))
def synced = folded('index-sync') ? map : gathered(index_sync(map))
def after = gathered(synced.mix(st))
if (params.post || folded('index-sync')) post(after)
if (params.over_time) over_time(after)
```

- `index-sync` (D1 footers: the map serves the scan) waits only on the map's inputs.
- `post` (warm-cache, probe, digest, index-gc, …) and `over_time` still wait on `st`, for these reasons:
  - **probe:** run before the append, it would count every `scan-not-indexed` refusal as a failure. It already turns all-lag failures into "search behind" (`behind_note`), but that should stay the exception.
  - **digest (Slack):** it links the scan, and its search links would refuse.
  - **warm-cache:** its filtered views would warm refusals.
- `index-sync` folded into `post` (`folded('index-sync')`) can't run early. With `map_first` on, `scan head` unfolds it (its own task) and warns if the config folds it; the check belongs in `_static_check` / the profile checks.

## While search lags

The site serves the scan on the map; `?f=` searches on it are refused (`scan-not-indexed`), as they are today whenever the append is behind. The refusal (the filter bar's notice) says so in plain words and links:

1. **The running job:** the scan's `/scans` row (its run, phase `static-names` running or busy), from the run recorded for the scan (D1 `scan_runs` by scan id).
2. **The last searchable scan:** the newest scan the static index covers (`/api/filter-scans`' newest), as a link to the same view at that `?d=`.

`/scans` already marks the run "search behind" (`phaseBehind`).

## Related

`specs/done/search-gated-default-scan.md` (built): per deployment (`DEFAULT_SCAN`), the default is the newest scan with its search box disabled until the append (`newest`), or the newest searchable one (`searchable`). With `map_first` on, either composes: the newer scan is reachable and labelled "indexing". Either can ship alone; this one is the job-side half.

## Tests

- `scan.nf` order: the Nextflow DAG test (or a dry `-preview` of the DAG) shows `index_sync` with no `static_names` upstream when on, and `post` downstream of both. Off: unchanged.
- The profile check: `map_first` with `index-sync` folded → the warning, and `scan head` unfolds it.
- Site: a refused search on an unindexed scan renders both links (exact text and hrefs).
