# Scan `marin-na` without mixing it into CoreWeave totals

Status: implementation handoff from the root disky agent, 2026-10-11. **Hosting recommendation superseded:** Ryan prefers exploring an OA R2 deployment grouped by the Cloudflare billing account. Do not enable `/na` or inherit CW viewer access from this handoff. Bucket ownership/use does not establish viewer eligibility. The job factoring and source/publisher credential separation below remain useful input to an independent deployment. No scan, token creation, IAM write, deployment or scheduler activation has been performed by this handoff.

## Scope and hosting choice

`marin-na` is one private R2 bucket in the OA account, used by the Marin CoreWeave clusters. The inventory is a historical observation, not a current measurement. Its roots include `finelog/`, `iris/`, `marin/`, `ot-agent/`, `tmp/`, and `users/`.

Prefer a separate store on the existing CW host because the audience and auth are shared. Do not add its bytes to the primary CW root, zone quota calculations, digest or sweep targets. A secondary store currently has no assignments/ownership or destructive actions. If those are required immediately, revisit an independent deployment or a shared-base extension before claiming feature parity.

Minimum first release: exact bucket/dir totals, treemap drilling, object counts, age, scan selection, and snapshot diffs once two scans exist. Text search is a separately verified capability: a path-store build alone does not establish static search support for secondary stores. Never advertise it as indexed or return false zero results without a working build/reader path.

## Reuse the job chain, not the meta identity

The current `job/cw-meta-run.sh` is a useful chain but not yet a generic store runner: it hardcodes `STORE=meta`, the webdata label `our storage`, work/publish identity and completion message. `cw-meta-submit.sh` likewise pins meta roots and shares source and destination R2 credentials.

Factor a shared secondary-store scan runner with explicit store key, source roots, root label, work directory, snapshot subdir and layer-2 prefix. Retain the meta wrapper's existing behavior. Add a small `na` wrapper/profile, rather than copying the entire runner and introducing another divergent pipeline. Follow the existing explicit-config/no-ambient-default direction; canonical scheduler specs must be independent of arbitrary caller environment.

For the recommended `/na` layout:

- Source: `r2://marin-na`, using its bucket-scoped read-only credentials and explicit endpoint.
- Derived snapshots: `snapshots/na/<scan>/`.
- Layer-2/index: `na-l2/<scan>/...`, in the existing derived-output storage, never in `marin-na`.
- D1: secondary store `na`, namespaced variants such as `na:path` and `na:bysize`. Confirm the existing store migration is already applied; do not invent a duplicate migration.
- FE: a store module under `site/src/stores/`, route `/na`, scheme `r2://`, viewer access, staging/owners off. Include it in `STORES_EXTRA` only when the first complete scan is available.
- BE: corresponding `STORES_JSON.na`, restricted to `na` derived prefixes. Origin object reads are not needed for metadata browsing. Do not enable a raw-file proxy using the derived-output key as though it could read `marin-na`.

Inspect how `SERVED_R2`, scan pointers and publish ordering now work before reusing the older meta script. Build both path and bysize sorts for general treemap drilling unless measurements justify otherwise. A failed build/sync/publish must not advance the visible scan pointer. Replace warning-and-continue behavior where it would expose an incomplete `na` scan.

## Credentials and external gates

Keep two distinct credential roles:

1. A dedicated consumer token with **Object Read only**, scoped only to `marin-na`, for listing. Inject into the lister's source credential context (`AWS_*` or an explicit source profile). Do not widen the existing publishing token or copy credentials into tracked config.
2. The existing derived-index publisher credentials, for `oa-cw-s3-usage-index`. Leave those in the publishing `R2_*` context.

Secret Manager names and accessor grants must be reviewed with the CW agent/user. Never print secret values, including through shell xtrace. Only the job needs source credentials; the site serves derived indexes. Optional lifecycle inspection may require more than object-read scope: verify support, and do not broaden the token just to render a lifecycle fold.

The user supplies/approves the source token, its Secret Manager versions and job access. Pulumi preview is allowed by the existing workflow; `up` remains the user's action. Notify the Marin owners that the proposed scan lists object metadata only. No object body/footer sampling or origin writes belong to this rollout.

## Acceptance and rollout

1. Exact tests for meta wrapper parity, `na` dry-run submit specs, canonical spec stability under unrelated environment overrides, source/publish credential separation, and per-store paths. Test a failure before publication and prove the visible pointer remains unchanged.
2. Exact FE/BE store routing and cache isolation tests. Primary CW and `/meta` responses/config remain unchanged; primary-only actions/ownership refuse on `na`.
3. With credentials available, run one manual remote scan; no heavy local build. Record listing/build/publication durations and derived sizes. Validate root bytes/counts against the listing, and representative drill totals, before publishing the store entry.
4. CIC the actual dev route, root, nested directory, scan selection and unavailable dates. Verify that displayed costs/quotas are not CW-zone quotas. Deploy only after acceptance and the relevant deployment go.
5. Run a second scan and validate unchanged/add/change/remove cases in snapshot diffs. Enable a daily cron only after the manual flow succeeds. Use the existing Batch/Pulumi cost labels and logs; include explicit completion/failure markers.

An independent deployment, if chosen, reuses this source/job work but gets its own site/D1/config/scheduler instead of `STORES_EXTRA`/secondary routing. Do not provision both alternatives.

## References

See [inventory], [multi-store], and the [R2 token documentation].

[inventory]: oa-store-inventory.md
[multi-store]: multi-store.md
[R2 token documentation]: https://developers.cloudflare.com/r2/api/tokens/
