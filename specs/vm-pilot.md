# VM pilot: r2 + hf served and scanned from one small always-on VM

Status: draft (2026-11-26, the cw session, from Ryan's ask: "get this working for r2+hf, on one small/cheap VM, where we'd nonetheless build out all the mechanisms"). For the root session to own (`cloud`, r2, hf). Builds on [vm-mode.md] (`vmd`, packs, tenants, the cell rule, parity: built and measured), [serverless-tiers.md] (the Worker ↔ VM hand-off, §5) and [full-scan-serving-bench.md]. Nothing here is approved for infra yet: the VM, its tunnel, secrets and Pulumi are each Ryan's go.

## Why

Serving the full filter cube (start/end, dir, query, user, class, ledger status) exactly needs every version row in memory: precomputed indexes cover a slice (the static name index answers rare terms, refuses common ones and any query combined with a user or class scope) and are the costliest part of each scan. GCP compute, last 14 days scaled to a month (billing export, 2026-11-26): static-names (append, catalog, drill, anchors, folds) cw ~$700, gcs ~$400; scans cw ~$290, gcs ~$400; gcs's interval store ~$270; r2 ~$36, hf ~$13. `vmd` answers every axis in 0.1–2 s from a memory-mapped pack (gcs: 32.4 GB).

The pilot proves the shape on the two smallest deployments, where nothing has to move off GCP: one always-on VM that **serves** every view from `vmd` and **runs** the deployments' scans in its idle CPU, under a fixed memory budget for scan work, with no per-scan indexing beyond the interval store and the pack.

## The box

- One VM, `deployment=shared`, `component=vm-pilot`: an e2-highmem-2 (16 GB) or e2-standard-4 (16 GB, 4 vCPU), ~$65–100/month; a 50 GB pd-balanced disk for packs, the work dir and the job runner's state.
- Memory budget (systemd slices; cgroup v2):
  - `serve.slice`: `vmd` and its packs (r2 + hf, each < 1 GB), `MemoryMin` = packs + headroom, `CPUWeight=10000`.
  - `scan.slice`: every scan job, `MemoryMax` = the scan budget (e.g. 8 GB), `CPUWeight=100`, `IOWeight=100`. A request never waits on a scan for CPU; a scan that needs more memory spills to disk, it never grows the slice.
- Packs mlocked (or `MemoryMin`-protected) so scan page cache never evicts them.

## Mechanisms (each one cw and gcs need later)

1. **A job backend interface.** Today `batch.submit_job` is the one GCP seam for most jobs; the static-names runners, Nextflow's executor and the site's REST submits bypass it. Define `JobBackend` (`submit(spec) → id`, `status(id)`, `logs(id)`, `cancel(id)`) with three implementations: GCP Batch (today's), `local` (a process in `scan.slice`, state on disk, logs to Cloud Logging or the store), Kubernetes Job (later, cw's CoreWeave nodes). The scan profile names its backend; every step's job goes through it.
2. **Bounded scan steps.** Each step the pilot runs takes its memory from the slice budget: listing (already streaming), `import -e stream` / `path-index` (DuckDB `-M`), the interval-store append (`-M`, `-B` sub-ranges). Measure each at the budget on r2 and hf; fix any that doesn't fit by spilling, not by more memory.
3. **Incremental pack updates.** Today `vm-pack build` rewrites the whole pack, loading every version row (117 GB peak on gcs). Instead the interval-store append's output (the scan's opened versions and close records) is folded into the tenant's pack: a delta file `vmd` maps beside the base pack, compacted into a new base on the interval store's own compaction. Memory then scales with a scan's changes, not with history.
4. **Serving wiring** ([serverless-tiers.md] §5, [vm-mode.md] §7): `vmAdapter.ts` called by `/api/subtree`, `/api/diff`, `/api/series`, search; the Worker reaches `vmd` through a Cloudflare Tunnel (HMAC per tenant, already built); `vmd`'s `/v1/status` copied to the store so the Worker knows the box's state; when the box is down or loading, the Worker answers what it answers today and says so (no silent partial answer).
5. **Retire per-scan indexing for the pilot's tenants** once `ps=iv`-style parity holds on their live requests: static-names off for r2 and hf, the site's `FILTER_STATIC*` flags replaced by `vmd`. This changes the parity baseline (CLAUDE.md "Deployments: parity by default"), so it's Ryan's call per deployment.
6. **Cost per step on /scans**, from the same budget: the box's cost is fixed, so a step's share is its CPU-seconds × the box's $/CPU-s; Batch steps keep their billed cost (see the cw session's per-step cost proposal).

## Scheduling

- Scans as today (the r2 demo's daily ingest; hf's cron), submitted to the `local` backend. A scan arriving while one runs queues (one at a time per tenant; the box's CPU is shared).
- Inverted cost model (Ryan's): the box's price is the compute budget; scans run as often as its idle CPU allows. The pilot measures that rate for r2 + hf.

## Order

1. `JobBackend` + `local`, the slices, one r2 scan end to end on the box (`vmd` not yet serving): its wall time and peak memory per step at the budget.
2. `vmd` on the box with r2 + hf packs (full rewrite per scan, as today); the Worker wiring on r2's dev stack; parity on live requests.
3. Incremental pack updates.
4. r2 prod serves from the box; then hf; then static-names off for both (Ryan's go).
5. Write up what cw (CoreWeave CPU nodes, Kubernetes backend) and gcs (memory: 32 GB pack) need beyond this.

## Open questions

- Does r2's public map tolerate the box's downtime (a VM restart, a pack swap)? The fallback is today's Worker path while it exists; after static-names is retired for r2, search has no fallback.
- hf's listing is rate-limited by the Hub API (429s at 8 in flight), not CPU: it fits the box's idle cycles, but a scan can take hours.
- Where the box runs: GCP (next to r2's and hf's Batch today) or CoreWeave (cw's free CPU nodes, next to the cw tenant later). GCP for the pilot; the backend interface keeps the choice open.

[vm-mode.md]: vm-mode.md
[serverless-tiers.md]: serverless-tiers.md
[full-scan-serving-bench.md]: full-scan-serving-bench.md
