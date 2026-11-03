# One append coordinator per lineage

Status: built on `cloud` (2026-10-22): `append_runner.coordinator_lease`, `Runner.coordination`. Asked for by cw (request (b), 2026-10-22).

## Problem

Nothing stops two `runs add` coordinators on one store at once (`static-names runs add`, `interval-store runs add`: both are `append_runner.Runner`). They race on the same work:
- cw is catching up its static index out of band (`job/cw-static-submit.sh`, a 48 h coordinator job), with `skip: [static-names]` in `cw.yml` so the scan doesn't start a second one. Dropping that skip before the catch-up finishes starts two coordinators, which both submit the same scan's Batch stages, both publish, and both prune.
- A scan's `static_names` task can outlive the next day's scan (it's capped at 6 h, and a backlog makes it longer), and then the next scan's `runs add -c` starts while the first is still running.

Today only the merge stage is serialized, by the merge lease (`<scratch>/<prefix>/<gen>/merge.lease.json`). Appends, backfills, `rerun`/prune and the R2 copy are not.

Codex wrote a guard on cw (`pipeline_guard` in `static_runner.py`, WIP snapshot 7bda907c). cw didn't take it because it was tied to Codex's compaction lifecycle (`static_lifecycle`, pinned generations). This spec keeps its mechanism and drops that tie.

## Design

A **coordinator lease** held for all of `Runner.run`, in the base `Runner`, so both stores get it.

- **Key:** `<scratch>/<prefix>/coordinator.lease.json`, where `<prefix>` is the store's prefix (`static-names`, `interval-store`), with no generation in the key. This is the "lineage": a coordinator that compacts or follows `CURRENT.json` into a new generation keeps the lease it already holds. A lease keyed per generation would let a second coordinator start on the new generation while the first is still finishing the old one.
- **Record:** `{owner, holder, at, ttl_s}`, the same shape as the merge lease. `owner` is a fresh uuid per `run`, and `holder` is `$DT_JOB_NAME` (else the host and pid), so a refusal says who holds it.
- **Take:** the same conditional create as `GcsRunStore.lease` (`if_generation_match=0`; a stale record deleted only at the generation read), over a small `LeaseFile` seam: `GcsLeaseFile` (generation preconditions) and `LocalLeaseFile` (tests). `CoordinatorLease` does take / renew / check / release over it. The merge lease keeps its own code for now; moving it onto `LeaseFile` is a later cleanup.
- **Renew:** a heartbeat thread rewrites `at` every `HEARTBEAT_S` (300 s), conditional on the generation it last wrote (Codex's `beat`). `ttl_s` is short, `COORD_TTL_S` = 3 × `HEARTBEAT_S`, because the holder renews it. A crashed coordinator then blocks the next one for at most ~15 min, rather than for a Batch task's lifetime as the merge lease does.
- **Lost lease:** if a renewal finds a different owner or a changed generation, the coordinator stops at the next `stage` boundary: `Runner.stage` checks before and after each stage, as Codex's did. The Batch job it already submitted keeps running, and it is safe because every stage is idempotent and post-checked. The coordinator raises `LeaseLost`, which exits 1: a real failure, since another coordinator replaced a live one.
- **Release:** in a `finally`, conditional on owner, as `release` does now.
- **Held by another:** `run` raises `Busy(holder, at)` before doing anything, and exits **4** (`BUSY`), a status distinct from `NOT_NEXT` = 3.
  - **Scan step** (`scan_steps.py` `static_names`, and the interval-store step): treat exit 4 as "another coordinator is appending; it will reach this scan". Log it as a warning and don't fail the scan. That other coordinator is either a `-c` catch-up, which appends every published scan, or the previous scan's run, which the next scan's catch-up covers.
  - **Hand runs:** print the holder and exit 4. There is no wait flag: a coordinator waiting minutes for a lease is the wrong shape. Re-run after it finishes.
- **Merge lease:** unchanged and still taken inside. The merge job runs on its own VM after a submit-and-go (`merge_wait` 0), so it can outlive the coordinator that submitted it. Its own lease is what serializes it against the next coordinator's merge stage.
- **Dry runs** (`-n`): take no lease (`coordination` is None), and don't report one.
- **Wiring:** `static_runner.gcs_runner` and `interval_append.store_runner` set `coordination` to `gcs_coordinator_lease(<scratch>, <prefix>)`. Tests construct `Runner`s without it.

## Out of scope

- Codex's compaction coordination (`compact_prepare`, the `static_lifecycle` pin/publish/prune, `--require-merge`). Static compaction is `static_compact.py` on `cloud` (`specs/static-compaction.md`); its own coordinator takes this same lease when it lands.
- Locking `static-names add` / `interval-store add`, the one-machine paths (the r2 demo's daily ingest). One GHA cron runs them, with `concurrency:` set at the workflow level.

## Tests

`LocalLeaseFile` with an injected clock (`test_append_runner.py`), plus `test_static_runner.py` for the stage boundary:
- **Exclusion:** a second `run` while the first holds the lease raises `Busy`, and the CLI exits 4.
- **Staleness:** a record older than `ttl_s` is taken over, and a fresh one is not.
- **Lost lease:** overwrite the record mid-run (a fake `stage` that swaps the owner). The next stage boundary raises `LeaseLost`, and nothing after it runs.
- **Release:** after a normal run, and after a raising stage, the lease file is gone. A release by a non-owner leaves it in place.
- **Scan step:** a `runs add` exiting 4 ends the phase ok (`test_static_names_busy_is_not_a_failure`). The staged interval-store step ends its phase 0 with a note.
- **Across generations:** holds by construction, because the key has no generation in it.

## cw

Once this is on `cloud` and in cw's image:
- Keep `skip: [static-names]` until the out-of-band catch-up finishes. Dropping it earlier is safe, but the scan's step only logs `busy` each day until then.
- `cw-static-submit.sh`'s job takes the lease like any `runs add`.
