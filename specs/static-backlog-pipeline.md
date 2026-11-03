# Static names: a backlog's scans pipelined, and an alert when the index lags

## Why

When the static index falls behind, the site's default view (the newest scan) has no search until the backlog drains. The index is scan-ordered: each run is a delta on the ones before it, and `runs add` refuses a scan that isn't next. So the newest can't jump the queue. What can change is how long the queue takes. Today `runs add -c` runs each scan's stages to completion before the next scan starts. On cw that's ~30–40 min a scan:

| stage | time |
|---|---|
| append | ~5 min |
| shards ∥ catalog | ~10 min |
| drill ∥ anchors | ~16 min |
| publish + R2 | ~5 min |

The cw outage that motivated this ran for days, and showed only as "partial" on `/scans`.

## Dependencies

Per scan N+1, each stage needs:

- **append:** append(N)'s open-version states. The states are pruned after N publishes, so they must survive until N+1's append has read them.
- **shards, catalog:** only N+1's own `cdelta/`.
- **drill:** N+1's shards, and every prior tier's `drill/`, N's included.
- **anchors:** N+1's shards and `cdelta/`, and every prior tier's `names/` + `anchors/`, N's included.
- **publish:** N's manifest. Publishing stays strictly in order.

So stage S of N+1 can start once stage S of N (and N+1's own earlier stages) are done: a pipeline whose period is the slowest stage (~16 min on cw), not the sum.

**The catch: implicit tier lists.** `prepare` (is this the next scan?), `append` (the open versions), `static_drill build` and `static_anchors run` all find the runs before a scan from the newest earlier manifest. With N's manifest not yet published, N+1 would be refused as "not next", or would build over N−1's tiers and miss N: wrong bytes, silently.

## Shape

1. **Pending listings (done):** `prepare` writes `pending/<scan>.json`: the runs before it plus its own level-0 run. Every "runs before scan X" lookup (`append_runner.prior_key`, used by `static_append._latest_manifest(before=…)` and `static_anchors._manifest_runs(before=…)`) takes the newest pending listing of a scan after the newest manifest before X, else that manifest. `publish` deletes the pending listings through its scan. Readers (the site, R2, merges, compaction) see manifests only.
2. **The pipeline (done):** `StaticRunner.steps(d)` is the chain as steps (prepare, append, shards ∥ catalog, drill ∥ anchors, publish, R2, verify, prune). With `pipeline_depth` > 1 (profile; `$STATIC_NAMES_PIPELINE_DEPTH`; default 1 until a deployment's catch-up has run on it), `run_scans` runs a backlog's chains in threads, `pipeline_depth` scans in flight. Step j of a scan waits for step j of the scan before it, so everything is in scan order, publishes included.
   - **Failure:** a failed step stops its scan and the later ones before their next step. Earlier scans go on, since they never read it. The earliest failure is raised. A rerun resumes per step as today.
   - **Carries:** they still run once after the backlog (the merge stage).
3. **State retention:** a scan's open-version states are pruned only after its publish (`prune` keeps the newest complete state). The next scan's append has already read them by then, because append(N+1) starts only after append(N).

## Lag alert

`scan-run supervise` (or the probe, `probe -c`) compares the static index's newest manifest scan with the newest published scan. If the index is behind by more than one scan, or by more than a set age (default 12 h), it posts to `#<deploy>-admin` once per lag episode (not once per run). It stays quiet until the lag clears and then rises again. `/scans` shows the lag beside "partial".

## Steps

1. Pending listings + `prior_key` (done).
2. The pipelined scheduler (`pipeline_depth`) (done). Tests:
   - 3 pending scans at depth 3 write the same jobs, manifests and merge as depth 1, every step in scan order;
   - the next scan's append runs while this one's drill is in flight;
   - a mid-backlog failure stops only the later scans.
3. A deployment turns it on (cw first: `STATIC_NAMES_PIPELINE_DEPTH=3`), then the default moves to 3.
4. The lag alert (done): `runs lag SCAN` (`lag_state`, `lag_episode`, the marker `<gen>/lag-alert.json` in scratch) after the static-names step's `runs add`, a warn alert at an episode's start. Still open: the `/scans` lag field.
5. `runs check-priors SCAN…` (done): each run's drill and anchors `prior` (anchors' `meta.json` records it now, as the drill's does) against its manifest's runs before it, and no `pending/` left. A pipelined catch-up's check.

## Open

- **Concurrent Batch jobs:** at depth 3 a backlog runs ~3× the jobs at once. Check quota (CPUs, local SSD) per deployment.
