# Reviewed staging, useful run details, and visible loading

**Moved to `specs/done/` 2026-10-22** (gcs's spec sweep, Ryan's go): Done: adopted on `cloud` as `1a529555` (from cw's `01d96e1b`) and merged into gcs and cw. The status line below predates the adoption.

Status: implemented and deployed to CW after explicit approval on 2026-10-21 (`2c3efece`). The isolated build and full site suite pass; live API/assets verified. Shared upstream adoption is requested. No commits or pushes have been made.

## Reviewed-state lifecycle

A staging gesture approves a particular snapshot. A successful real run consumes that reviewed staging entry even if newer or replaced objects remain under the prefix. Those objects stay untouched, their preservation belongs in the run's analytics, and including them requires the ordinary review and new staging flow. The staged page must not offer a shortcut that silently expands an earlier approval.

`unstageDeleted` now requires explicitly verified successful run ids, checks the original item predates the run, excludes dry runs, invalidated summaries and restored items, and audits removal idempotently. Both executor job readers pass successful provider ids; completion announcements only settle successful results. A finished timestamp or a result file alone is not proof of success. CW reflection checks `delete_failed` and the provider outcome; GCS reflection preserves failed status even if the executor wrote final totals.

Strict terminal reflection means the task's own early exit-hook poll can precede provider completion. Final reflection/announcement then occurs on the next job poll. Preserve this distinction when adapting upstream; an out-of-process completion watcher is needed for a guaranteed unattended terminal notification rather than treating the running task as already successful.

Completed batches retain original staged prefix names from the existing audit trail. Their folded history can be expanded and linked by batch fragment. Labels describe completion of a review, without implying every current object below the prefix was deleted.

## Run details and analytics

The dedicated run page renders D1 totals and per-prefix outcomes independently of run-file browsing. It shows elapsed time, deletions, already-gone keys, preserved keys, and honest provider-specific recovery information. S3 runs must not display GCS CLI commands or promise a recovery window from a legacy run-level deadline.

Per-prefix outcomes combine preservation into one sortable `skipped` column, with labeled and differently colored `added` and `changed` counts. A reconstructed snapshot split is identified in the tooltip; counts with no matching reconstruction remain explicitly unclassified. The underlying numeric sort uses the total skipped count. `changed` includes replacements, rather than implying an in-place edit.

Optional review/execution artifacts load separately through `/api/plans/run-analytics`; they do not block `/api/plans/run` or its D1 statistics. CW exposes the manifest's planned totals and original review cutoffs, and the execution's failure count and preservation breakdown. A missing artifact is distinct from an HTTP/authentication failure. The latter is shown as an analytics error while base statistics stay usable.

The Python S3 manifest builder now writes an immutable `manifest/<bucket>-preserved.parquet` mapping excluded keys to `new_since_review` or `changed_since_review`, and counts each in `plan-summary.json`. Execution distinguishes those from `new_since_scan`; older manifests without the sidecar explicitly report `unclassified`. Existing opaque `version` checks remain in effect. This Python change was added after the earlier ETag/CS3 candidate image was built. A separate analytics-only overlay of the existing production executor has now built and verified successfully; it has not been promoted. GCS should adopt equivalent classification at its own manifest/executor boundary, preserving native generation preconditions.

`dt-cloud plan-sweep analyze-preserved -d SCAN -I HISTORICAL_STORE -o NEW_LOCAL_DIR RUN_ROOT` reconstructs legacy manifest-time exclusions from the original review and manifest scans. It verifies reconstructed eligible rows exactly against the saved manifest and omitted count/bytes against the original summary, hashes its inputs, and refuses to overwrite an analysis. It never invokes deletion. Original run evidence stays intact; a supplemental `preservation-analysis.json` can be attached separately. The run analytics endpoint and UI display that snapshot reconstruction with provenance, independently of executor observations: the legacy executor did not save names outside its manifest, so an equal execution count does not prove identical execution-time membership. This has been exercised against the completed CW run, with the supplemental artifact published and read back. Private production-specific findings remain in `tmp/`.

## Fast staging and shared loading motif

The staged-path request must not wait on prefix sizing, history, or live progress. Show its returned paths immediately, with separate progress for the data still loading; errors must stop spinning. Independent D1 reads in `planDetail` run concurrently. Finished runs stop background polling.

Use the existing `Busy`/`Skeleton` components for widget loading: a reserved initial area, or the previous widget faded beneath a centered frosted overlay and spinner. `Loading` supplies a compact spinner plus an action-specific status label for text/table contexts. Background refinement uses a corner indicator. Error, unavailable and empty states do not animate. Respect reduced-motion preferences.

The CW changes cover the map's first paint, diffs, staged paths and sizing, run details and logs, name summaries, route suspense, admin/session views, token loading, lifecycle rules and bulk-match listing. Review other deployments' loading contexts during adoption; retain their own layouts and store configuration.

## Handoff and validation

The session-log switch also supports no expiration through its admin checkbox. `PUT /api/session-log/switch` with `enabled: true, until: null` enables it until turned off; omitted expiry keeps the finite default. Storage uses `until_ms = 0` for enabled without expiration and NULL for off, so no schema migration is needed. API reads return `enabled: true, until: null`; both the boot shim and logger accept that form, and a server-side 503 still stops the client. Finite dates retain their original bounds. The UI can change between finite and indefinite modes and persists the selection with "Save expiration". CW's live switch is now indefinite. Shared code is in `SessionLogSwitch.tsx`, `sessionLog.ts`, `sessionLogBoot.ts`, `sessionLogShape.ts`, the session-log API and their tests; `specs/done/session-log.md` documents the updated contract.

The CW worktree (`wt/cw`) contains the shared changes under `site/src`, `site/functions/_lib`, `site/functions/api`, and `cloud/src/dt_cloud/{sweep,sweep_analysis,sweep_source}.py`, plus registration of `analyze-preserved` in `cli.py`, with corresponding tests. `tmp/cw-reviewed-loading-site-files.txt` inventories the isolated site candidate. Adopt coherent shared hunks, retaining unrelated upstream changes and deployment-owned configuration. The earlier unresolved staging fixes (interval-based prefix sizing and audit-backed batch names) are included.

Regression coverage includes reviewed-staging consumption despite newer objects, preservation of unrun/later-staged/dry/invalidated work, terminal failure propagation, a run page without file access, prefix-reader generation resolution, original batch names, new-versus-changed/post-scan analytics, verified supplemental backfill, and indefinite logging plus turning it off. The isolated site build succeeds. The final full site suite passes: 2,596 tests passed, two skipped. All 32 Python sweep/backfill/source tests pass, and the analytics-only production-executor overlay passes 19 tests. Exact test summaries and deployment evidence remain in private `tmp/` logs. No browser is connected for extension-based CIC. A local preview backed exclusively by saved run artifacts rendered successfully in headless Chromium, including the combined skipped column; the admin checkbox was exercised with its PUT intercepted locally. Production API reads and exact deployed entry assets were verified; screenshot evidence is in `tmp/`.

Deployment-specific applies and cutovers remain with their owners. The already completed source-object deletion is not to be retried. The one remaining reviewed CW staging entry was completed separately, with an audit record, without deleting its preserved objects.
