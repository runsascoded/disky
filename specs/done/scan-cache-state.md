# Show reused tasks in scan retries

**Moved to `specs/done/` 2026-10-22** (gcs's spec sweep, Ryan's go): Done on `cloud`: `8f27e690` (from cw's `681ff1bc`): `/scans`' DAG shows Nextflow cache hits as "reused". Deploys are per deployment.

Status: implemented and verified locally on 2026-10-22. Production Pages deployment was rejected by automatic approval review pending explicit approval. The job-image changes have not been deployed; the running retry retains its existing image. No commits or pushes.

The pipeline diagram joined nodes only to phases recorded by the current attempt. Nextflow cache hits emit no new phases, so a resumed pipeline showed already completed source listing, indexing, publication and interval work as waiting. Inferring successful nodes from missing phase records would hide real unknown or pending states.

The head now reads explicit `CACHED` task names from its live Nextflow trace and refreshes the stored graph when that set changes. The engine-neutral graph carries optional `cached_tasks`; the graph reader also includes the sibling trace at final publication, preserving cache state after completion. It does not invent phases, timings, outputs or success records. An incomplete trace row is ignored until complete. Status publication does not alter the process execution or retry policy.

The diagram distinguishes reused nodes from running and waiting nodes, with a task count for fan-out nodes and a tooltip explaining that previous execution time is not added to the current attempt. Mixed processes retain their actual phase status and expose their reused task count separately. Missing cache evidence retains the existing pending/unrecorded behavior.

Validation: 89 Python tests across the DAG reader, head and scan-run writer; 24 site model tests; TypeScript/production build. A head test verifies live cache publication before process completion without duplicate writes on unchanged polls. Headless browser rendering of the saved production retry with explicit cache evidence shows five reused nodes, one running node and two waiting nodes, with zero page errors. No Chrome connection was available. Private logs, the rendered screenshot and an isolated site build based on the last production release are under `tmp/`.

Deployment handoff: deploy the isolated site changes, rebuild the job image with the shared writer changes, and backfill the current retry's graph from its recorded `Cached process` head-log lines. Preserve unrelated changes in `scan_runs.py`. A currently running old head can replace the graph at finish, so its final graph needs the same backfill if it completes before the new writer is in use.
