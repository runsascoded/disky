# CW infrastructure isolation

Status: the operational CW cutover is complete and verified. Ryan approved the isolation plan and remaining reviewed non-destructive isolation applies, including the explicit public-PNG destination approval. The approved CW history and latest complete scratch state are copied and checksum-verified; source manifests remained unchanged during the writer drain. Preview and production now use CW-owned configuration and bucket-only read-only R2 credentials. Both scheduler bodies are updated and schedules resumed. The isolated meta validation run, first six-hourly scan on isolated targets (`2026-10-13T1201`) and its background static-name compaction succeeded. Live serving checks pass. Migration source is committed as `1e47685c`; merge `f20e5c0f` includes shared `cloud` through `362ca1a6`. Ryan subsequently authorized safe cleanup/revocation: six obsolete CW IAM bindings were removed, with no data, containers or credential versions deleted. Old resources/data/credentials remain, but rollback requires restoring the removed bindings first. Both persistent `prepareIsolation` flags must stay enabled. CW access logging stays on ice.

## October 13 obsolete-access cleanup

After auditing CW consumers and checking no active CW jobs in either configured region, `663c54bd` sets persistent `gcs-usage-gcp:retainSharedAccess: "false"`. Shared helper `0d87420c` adds selective accessor grants without removing protected secret containers. The reviewed saved plan proposed exactly six IAM deletes and 43 unchanged resources: CW access to the old shared data and scratch buckets, the shared `cf-pages-token`, the old CW R2 key-id/secret containers, and the project-wide Artifact Registry reader role. [Revocation apply] completed those six operations; [Revocation refresh] reports 43 unchanged. Live policy readbacks contain no removed CW bindings; the new repository-scoped reader remains. [Post-revocation serving] verifies authenticated history/tree/search/diff/series and repeated tree responses. The separate bounded post-revocation Batch smoke `cw-isolation-smoke-20261013-152616-64000e` succeeded, using the real job account and immutable image against new targets; [Post-revocation smoke] preserves its final state.

No source objects, secret versions/containers, service accounts, CF tokens, shared image repositories or historical icons were deleted or rotated. Recovery of removed access uses `retainSharedAccess: true` followed by a reviewed preview/apply; do not revert the whole pipeline blindly. Removing old CW payload copies still requires a distinct generation-pinned deletion manifest and authorization. Independently owned Pages/D1 credentials remain account-scoped, not a per-project/database security boundary. The old GCS sweep executor's `cf-pages-token` reference is not a CW dependency: CW uses `plan-sweep` through `cwBatch.ts`. [Post-revocation task logs] confirm the smoke's profile, both isolated GCS writes/reads, Pages/D1 and isolated R2 publisher checks all passed; only tiny retained canaries were written.

## Ownership and evidence

CW owns `cw-s3-job`, `cw-s3-dispatch`, its scan/meta schedules, CW Secret Manager containers, `oa-cw-s3-usage-db`, and the `oa-cw-s3-usage-index` serving bucket. Source config already sets `legacyJobAccess: false`; the preview leaves existing IAM unchanged. This is not proof that every historical GCS grant or project-wide role has been removed: audit project/service-account/secret policies before a later revoke.

Live GCS metadata confirms CW's job has bucket-wide `roles/storage.objectAdmin` on the GCS-owned `oa-gcs-usage-dvx` and `oa-gcs-usage-scratch`. Both are `US-EAST1`, labeled `deployment=shared`. CW also consumes `cf-pages-token`, the shared image repository `cloud-run-source-deploy/gcs-usage-snapshot:cw`, and the GCS icons Pages project. The shared bucket must not be copied wholesale: its `listing/`, `access/`, unqualified snapshots/digests/probes, other static generations and interval stores are not established CW ownership.

Private evidence is in ignored [bucket/IAM inventory], [namespace inventory], [preview], and [saved plan]. The initial namespace audit read metadata only. Subsequent checks read bounded control JSON and privately resolved a credential identifier for the policy lookup; no fleet/parquet data was downloaded and no credential values were printed or stored in the evidence files.

Update: the reproducible `job/cw-isolation-inventory.py` now captures exact object generations, sizes, checksums and storage classes, CW D1 index/over-time/deletion references, and bounded static control JSON references. Its 24 exact unit tests and the 16 infrastructure tests pass. The refreshed private [verified inventory] resolves every D1 index pointer to its exact parquet file, with no missing files. This is a candidate manifest, not an atomic cutover snapshot or copy authorization: drain writers and refresh immediately before migration. It excludes noncurrent/soft-deleted versions; review any rollback/audit requirements for those separately. Only bounded control JSON bodies were read, not parquet/fleet data. Copy quantities/costs stay private in [copy cost estimate], not this public repository.

The actual recent CW Cloud Build runs as the project's default Compute service account and has `roles/cloudbuild.builds.builder`; its live role includes Artifact Registry upload/download permissions. It can publish to the proposed repository without another project-wide grant. Initially retain this shared GCP platform builder, not a GCS-owned deployment job identity; a future dedicated CW builder is a separate hardening change, with source-staging/logging/actAs requirements. Its source archive uses the project's Cloud Build bucket. Do not revoke a platform builder role on behalf of GCS. Evidence: [build identity], [build IAM], [builder permissions].

Live Cloudflare policy inspection confirms the existing CW R2 token grants read/write to the **whole OA account**, not just the CW index bucket. Rotate to separate CW-index reader/publisher tokens before calling this a scoped credential cutover; keep the old token until both site and jobs validate with the replacements. New R2 credentials can use bucket-item resource scopes. D1 and Pages permission groups advertise account scopes instead: separately owned tokens enable independent rotation, but are not proof of per-database/project authorization. If a strict security boundary is required, design a scoped publishing proxy or separate account upstream; do not invent unsupported resource selectors. See [CF permission evidence] and [CW R2 policy].

## Additive foundation

`infra/gcp/cw_isolation.py` is called by CW's existing stack only when `gcs-usage-gcp:prepareIsolation` is true. It does not create another competing Pulumi state or alter current cron dependencies/body inputs. The persistent stack flag is now true and must stay enabled after resource creation.

| Resource | Intended owner / policy |
|---|---|
| `oa-cw-s3-usage-data` | CW stack; private, uniform access, protected, no force-destroy, no automatic data expiration, seven-day soft-delete recovery |
| `oa-cw-s3-usage-scratch` | CW stack; private, uniform access, protected, no force-destroy, seven-day expiration, soft delete disabled |
| `us-central1/cw-s3-usage` Artifact Registry repository | CW stack; protected, CW job reader; existing platform build-publisher permissions verified |
| `cw-s3-cf-pages-token` Secret Manager container | CW stack; protected, CW job accessor; independent Pages/D1 token installed as version 1 |
| `cw-s3-isolated-r2-access-key-id`, `cw-s3-isolated-r2-secret-access-key` | Protected CW containers/accessors; bucket-only publisher pair installed without changing old `latest` refs |
| Dispatcher data-bucket grants | `objectViewer` + `objectCreator` on CW data only; plan creation/result reads without deletion permission |

Every resource with labels carries `app=disky,deployment=cw` plus its component. Grants use additive IAM members, not replacement policies. Data/scratch locations deliberately retain `US-EAST1`; this isolates ownership without mixing in a provider/region relocation. Existing job/dispatch identities, billing project and CW serving R2/D1 stay the same.

At foundation creation, live writers still used old targets: empty resources alone did not enable the new pipeline. The later authorized cutover is recorded below. Storage copying temporarily duplicates the approved history; the reviewed quantities/costs stay in private evidence. The data bucket deliberately has no retention deletion; review its ongoing footprint independently. The scratch lifecycle really deletes old objects even though the bucket is protected.

## Artifact migration allowlist

Preserve the same keys, scan IDs and generation IDs. Resolve references and record source generations/checksums before copying; use remote/server-side copies, never a laptop download of the fleet.

| Current data-bucket namespace | CW use / action |
|---|---|
| `cw-l2/` | Canonical bucket layer-2, pointed index generations and over-time groups; inventory D1 references before copying/pruning |
| `snapshots/cw/` | Primary tree/age/meta/lifecycle JSONs; scan dropdown, digest and warm-cache readers |
| `digest/cw/` | Monthly Slack thread/reply convergence state; preserve it to avoid duplicate posts |
| `probes/cw/` | CW cold-serving observations |
| `cw-sweep/` | Historical CW sweep manifests/audit artifacts; preserve safety evidence |
| `sweep/cw/` | Current CW plan-sweep audit namespace, proven by `planDispatch.ts`/`cwBatch.ts`. Do not copy GCS's separate `sweep/runs/` |
| `meta-l2/`, `snapshots/meta/` | Current CW `/meta` outputs; verify ownership against CW D1 and known producers, then preserve history. After cutover, new meta scans must cover CW-owned data plus CW serving R2, not the whole GCS fleet |
| `static-names/2026-10-10cw/` | Current profile/site generation: base artifacts, runs, manifests, append/compact references; validate all reachable dependencies and embedded bucket/layout references |

Scratch contains `static-names/2026-10-10cw/`, older `2026-10-09cw/`, GCS generations, a compaction trial, an interval generation and drill profiling. Do not copy all scratch or abandon live leases/stages. Drain the CW stages, identify durable resumable state versus disposable shuffle, and create a fresh CW-only scratch namespace. Old CW generations/phase-0 experiments are not implicitly deleted or copied; retain them in place until ownership and retention are reviewed.

The live `interval-store/2026-10-09/` and `2026-10-09b/` namespaces are **not** in this automatic allowlist. CW's current profile/site does not select an interval generation. If a future CW cutover enables one, identify its producer and follow all manifests/runs/revisions/leases explicitly; do not infer ownership from a generic directory name. Likewise `static-names/src/` must be traced for shared dependencies before any revocation.

The initial pinned control audit found embedded old-data-bucket references at `/bucket` in the static generation's base `scans.json` and twelve per-scan delta `scans.json` files; the refreshed inventory includes subsequent scans too. A blind byte-for-byte copy leaves those records pointing at the old bucket. The migration CLI now implements an explicit destination-only transform allowlist for `/bucket` and `/scans/<i>/generation`, leaving originals untouched and rejecting other embedded old-bucket references. It preserves every other parsed field, records original/transformed SHA-256 digests, and copies unchanged manifest bytes verbatim. Do not change parquet payloads, scan IDs, generation IDs or unrelated JSON strings. Manifest generations in the inventory identify **source** objects: GCS assigns new generations to copies. Rebind source-generation checks to verified destination generations. Re-run the manifest/range/append/compact dependency checks before old-access revocation. A fresh bucket also resets object creation times seen by `/meta`; the original timestamps inside listing/index parquet payloads remain unchanged. `derived_from` names historical provenance, not a runtime dependency (the only source reference in shared code is its writer); preserve it unchanged.

Scratch open-state and merge leases are not serving artifacts, but may be required by the next append/merge. The private scratch manifest is for ownership/resumability review, not an instruction to copy all shuffle or recreate an active lease. Drain stages and explicitly preserve or rebuild only the needed state.

## Migration tooling checkpoint

`job/cw-isolation-migrate.py` provides four explicit phases: `controls` (bounded, pinned JSON reads), `audit-controls` (local identity checks without re-fetching), `copy` (metadata-only by default; optional generation-pinned server-side rewrite), and `publish-controls` (requires a complete verified payload journal, rechecks destination revisions, publishes remapped scans before manifests). Actual cloud writes require `--apply --writers-drained`; the flag is an operator attestation, not an automated drain. Check all active writers and leases, pause the CW scan/meta schedules and capture a fresh inventory before using it. No task or scheduler has been paused for this preparation.

The refreshed private [migration inventory] resolves all CW D1 index pointers. All 35 audited control documents' input generations/sizes/MD5/CRC32C match that inventory. A live metadata-only [copy plan] found every payload absent in the new data bucket and no incompatible destinations. It deferred all control JSON. No payload/control objects have been copied. The CLI is resumable against an identical inventory: matching destinations are verified rather than rewritten; mismatches fail closed, and destination writes use generation-zero preconditions. Do not seed mutable digest/control state and resume the old writers as if the journal were a final cutover snapshot.

Payload copy verification alone is not cutover readiness. The control publisher's completion only establishes readiness for reader/dependency checks; scratch resumability, credentials, image/config wiring, old/new views and a complete scheduled scan remain later gates. Source objects and credentials are retained for rollback.

## Additive Cloudflare phase — applied and verified

CW's `infra/cf/cw_stack.py` invokes the unchanged shared stack and optionally adds `cw_cf_isolation.py`. No shared stack/component file is modified. The persistent flag is enabled; all 11 preexisting resources remain. Root acknowledged this wrapper and will not duplicate edits; generic hoisting can follow review.

| New resource | Policy |
|---|---|
| `cw-isolation-reader` account token | R2 bucket-item read only, restricted to `oa-cw-s3-usage-index` |
| `cw-isolation-publisher` account token | R2 bucket-item read/write, restricted to `oa-cw-s3-usage-index` |
| `cw-isolation-pages-token` account token | D1 read/write + Pages read/write for the OA account; independent identity/rotation, **not** per-project/database isolation |
| `oa-cw-s3-usage-icons` Pages project | Protected standalone assets; arrows on `main`, three available public plots on `cw`; source/readback SHA-256 verified, no DNS/old icons changes |

All four resources are protected. Token values are secret outputs encrypted by the existing CF Pulumi state backend; never print them or put them in Git. GCP still manages credential containers/IAM only. Deliver new values to Secret Manager/Pages only after policy/read/write verification and preserve the existing live token until both jobs and site are validated.

`infra/cf/run` selects/trims `CF_IAC_TOKEN` without printing it. The ambient app token cannot list account-token permission groups (403/9109). [The approved CF plan] applied with four created/11 unchanged, no updates/deletes/replacements; [the CF post-apply preview] reports 15 unchanged. `job/cw-isolation-credentials.py` compares live policies exactly, verifies reads and optionally writes a tiny retained canary. `install-job` accepts only the three new secret names, refuses mismatched values and validates CRC32C/readback. Payloads pass from encrypted CF outputs to Secret Manager in process memory, outside Git and GCP IaC state, and are never printed.

Keep `gcs-usage-cf:prepareIsolation: "true"` enabled in `infra/cf/Pulumi.cw-s3.yaml`. The additive phase did not change old R2 policies or live bindings. Reader delivery and site/scheduler cutover subsequently completed as recorded below; old policies remain for rollback.

## October 13 preparation checkpoint

Cloud Build `7c5fc0b2-daa7-4c02-abe3-59903a1fe920` published `us-central1-docker.pkg.dev/oa-internal-450019/cw-s3-usage/scan:cw`, digest `sha256:4e53f34a042122498d9c9762c6ccf4b85fd2887f215575a3cf1bde922ed9c413`. [Build log] preserves the result. [Batch smoke] ran that immutable image as `cw-s3-job`, checked its profile, wrote/read retained new data/scratch/R2 canaries, and verified Pages/D1 credentials. It succeeded; this is not a complete production scan.

Local wiring covers scheduled/one-shot jobs, static data/scratch/image/secrets, scan-run output locations, digest asset hosts and both site environments. Tests inspect emitted JSON. Live cron/site configuration still uses old targets; do not manually submit new defaults against incomplete history. Inventory needs `--source` after profile changes. [October 13 pre-drain inventory] has no missing D1 index files, no deletion-run rows and an empty `sweep/cw/`. Scratch contains latest state with 64 `copen` files and 64 completion markers; validate pinned ranges/manifest before preserving only that state. Do not copy shuffle/claims/leases or treat pre-drain inventory as final.

Validation: 89 local isolation tests, 13 deployment-config/digest tests and 20 infrastructure tests pass. Credential delivery rerun verified existing versions without adding versions. `https://oa-cw-s3-usage-icons.pages.dev/arrows/av_deg50.png` matches source bytes. A bounded pinned audit verified all 64 latest-state completion markers and their matching files; its preservation plan excludes all other scratch objects. The refreshed full GCP preview contains exactly two scheduler-body updates and 47 unchanged resources, no creates/deletes/replacements; it is not applied. Historical payloads are not copied; neither scheduler is paused.

## October 13 live cutover checkpoint

After explicit public asset approval, three existing public plots were copied without changing filenames or bytes; readback from `cw.oa-cw-s3-usage-icons.pages.dev` matched SHA-256. Two historical September plots already returned 404 at the old host; they were not regenerated or published from private state. Neither old icons deployment nor private digest state was changed by the asset migration.

The CW scan/meta schedules were paused with no active CW Batch jobs in the two configured regions. [Drained inventory] pinned the approved history and all 37 control documents' input references. [Payload journal] verifies every non-control payload; [Scratch journal] verifies only the newest published state's 64 parquet files and 64 completion markers. Shuffle, claims and leases were excluded. [Published controls] rebind only the allowed bucket/input-generation fields and preserve other document contents. A second source inventory matched both data and scratch manifests exactly before cutover; old source data is untouched.

Preview deployment `e7200152` and production deployment `08d8972a` ship the uncommitted working tree, not an implied Git checkpoint. No commits, pushes or local deploy-pointer changes were made. Read-only pairs were delivered through process stdin, not arguments or plaintext secret files; deployment logs were checked without printing values. [Preview live checks] and [Production live checks] verify exact old/new scan histories, latest/oldest metadata, current/historical trees, static-name filtering, diffs and series, normalizing only the explicitly configured dev root label. [Production cold check] verifies a genuine cache miss followed by a hit on the new immutable deployment, with tree JSON matching. The OA Chrome profile rendered root, filter results and the new production `/meta` snapshot. Site tests: 1,881 passed, one skipped. Local isolation tests: 100 passed.

[Reviewed cutover plan] proposed only two scheduler `httpTarget.body` updates, 47 resources unchanged, with no creates/deletes/replacements. [Cutover apply] completed those updates while both jobs remained paused. Live bodies select the CW repository, CW data bucket, independent publishing secret and bucket-only R2 publisher pair; meta roots select only CW data plus CW index. Both schedules were then resumed. [Post-apply preview] reports 49 unchanged resources.

The manually triggered meta job `job-18b3dcbd-934b-4809-be12-c525deb59974` succeeded, publishing scan `2026-10-13T1145`. [Meta task logs] show successful indexing/D1 sync and R2 publication, no warning/error markers. [Production meta view] renders exactly the two CW-owned roots. Its historical chart's downward step is a scope change, not reclamation or deletion of the old GCS data. A complete scheduled primary CW scan remains the final end-to-end verification gate. Access revocation and removal of old CW copies remain separately authorized follow-ups.

The 12Z scheduler created `job-f4527d8b-f524-48c6-b9a8-cbf42c5e5b78` at `12:00:01Z`; its live body uses CW data and the CW repository. [First isolated scan] records `2026-10-13T1201`; the parent finished at `13:36:00Z`, and [Final Batch state] records success at `13:36:04Z`. [Final phase log] confirms all five listings/imports, index write, static append/shards/catalog/drill/R2 publication, D1 sync, primary R2 publication, over-time, index GC, warm-cache, serving probe and digest. The digest edited its monthly OP, posted the settled October 13 reply, removed the provisional reply and deployed its plot to the CW-only icons project. No warning/error markers appeared in the phase-filtered log. The existing `dt-cloud job watch -i 60` preserved [Scheduled scan watch]; no duplicate manual primary scan was submitted.

[New scan live checks] verify preview/production scan history, metadata, current/historical trees, search, diff and series, with only the configured dev root label normalized. The OA Chrome profile rendered [New scan view]. Warming completed 791 of 792 requests: one `hero-checkpoints` subtree request returned HTTP 503. [Exact warm-request retry] succeeded with the job grant; all 16 responses in [Cold serving probe] returned HTTP 200. This transient is recorded rather than treating Batch success as proof of perfect warming.

The append scheduled background compaction `sn-merge-2026-10-13-1201-131924`; its live spec uses CW data, CW scratch and the CW image repository. [Static compaction watch] records success; [Static compaction result] confirms shard/catalog/drill construction, artifact upload and publication of `manifests/2026-10-13T1201.m001.json` at `13:44:00Z`. This exercises both append and compaction after the scratch/input-generation migration. The parent intentionally does not wait for this merge, whose GCS revision is published to R2 by a later scan. Old-access revocation and deletion of old copies are not authorized by this successful cutover. The upstream OG/UI rollout is the next owned deployment task; no Git commit or merge was performed for this migration.

## Wiring checklist and gates

1. **Complete:** foundation, publisher-secret containers and dispatcher grants applied under Ryan's authorization. Keep `prepareIsolation: true` in both stacks; disabling it proposes removal. Authorization covers remaining reviewed non-destructive isolation applies, not unexpected changes, deletion or revocation.
2. Review and install a dedicated Cloudflare publishing token outside Git/state. Determine current D1 and Pages permission granularity rather than claiming a container name establishes CF-side scope. The job needs index/scan-run D1 access and digest plot deployment. Move icons/plots to a CW-owned project or document an explicit remaining shared service; preserve historical avatar/plot URLs during transition. Audit/rotate existing CW R2 credentials separately if their underlying policy is account-wide. Root's generic new-token scoping change belongs upstream.
3. Grant the actual Cloud Build publisher scoped write access to the new repository, build the reviewed CW image there and record its digest. Update all scheduled/one-shot/image consumers together; validate a pull as the CW job account. Do not remove its current project-wide Artifact Registry reader role until all repository consumers are checked.
4. Drain in-flight scan/meta/static-name/sweep jobs; capture the migration manifest, reachable D1/static references, counts/checksums and rollback configuration. Copy only approved CW artifacts remotely; verify immutable source generations, destinations and references. Never overwrite an existing incompatible destination or delete the old copies implicitly.
5. Update **all** `job/cw-*-submit.sh` defaults, `cw-run.sh`, `cw-meta-run.sh`, reindex/over-time defaults, `job/build.sh`, `job/static-names.json`, `job/scan-runs.json`, and both production/preview sections of `site/wrangler.toml` (data, sweep image, `/meta` roots/title). Update secret references from `cf-pages-token`; ensure `PIN=1 DRY=1` cron bodies reflect the new targets. Inspect shared CLI defaults and static/manifest JSON for old absolute bucket references, not just shell environment overrides. Preserve applied migration names and D1 lineage.
6. Review the scheduler wiring plan and apply only after migration/reader checks. Verify old/new history, `/meta`, static search/drill, subtree/diff/series, sweep dry-run, health, digest convergence and a **complete scheduled scan** under new wiring. Dev shares production D1; it is not a migration sandbox.
7. Only after proving no CW readers/writers remain on old data/scratch/secret/image targets, preview and separately authorize revocations. GCS retains its buckets and all its data; removal of old CW copies needs a distinct reviewed deletion manifest. Rollback uses retained old targets and grants plus the previous image/config/digest. If restoring digest state after new posts, reconcile it rather than blindly replaying an old state file.

## Reproduce checks

Use the existing root infrastructure environment (the ignored `infra/.venv` symlink here reuses it), not another dependency install:

```bash
infra/.venv/bin/python -m pytest -q infra/tests/test_cw_isolation.py infra/tests/test_cost_labels.py
direnv exec . pulumi -C infra/gcp preview --stack cw-s3 --config-file "$PWD/tmp/cw-isolation-preview-config.yaml" --diff --save-plan "$PWD/tmp/cw-isolation-preview-plan.json" --suppress-outputs --non-interactive --color never
```

The original pre-approval preview used an ignored alternate config; authorized applies use persistent normal configuration. Keep plans private. `--config` writes configuration, unlike alternate `--config-file`.

Inventory command (metadata plus bounded control JSON reads; output directory must not already exist):

```bash
direnv exec . job/cw-isolation-inventory.py -o tmp/cw-isolation-inventory-<checkpoint>
direnv exec . pytest -q tests/test_cw_isolation_inventory.py
```

The first foundation apply is complete. It created the eight empty resources and their additive grants, not credentials or new cron bodies. The persistent flag is enabled; [the refreshed foundation preview], [foundation plan], [foundation apply log] and [post-apply live preview] preserve the checkpoint. The applied command was:

```bash
direnv exec . pulumi -C infra/gcp up --stack cw-s3 --plan "$PWD/tmp/cw-isolation-foundation-plan.json" --yes --non-interactive --suppress-outputs --color never
```

Keep persistent flags enabled thereafter. Foundation refresh verified resource/grant settings and old cron inputs. CF resources and publisher credentials are installed; remaining gates are remote copy/control transforms and scheduler/site cutover. These are separately verified phases, not a blind switch. Do not revoke access or delete old copies before their later gates.

## Post-rebuild scheduled-scan checkpoint

October 12's 12Z cron job `job-aab7d92f-0b01-4f70-824e-daa3405376a1` ran image digest `sha256:22c27c70f41f952a710fc8c0cdf210ba265c0dffc5ad5dd03cb98fbb6337b427`, matching the rebuilt CW image. It completed all five bucket listings/imports, webdata, layer-2, index write/static-name append/index sync, R2 publication, over-time/GC, warm/probe and digest. Scan `2026-10-12T1201` finished at 13:37:18Z; Batch marked it succeeded at 13:37:21Z. The digest edited its monthly OP, posted the settled October 12 reply and removed that day's provisional reply. No warning/error markers appeared in the phase-filtered log. This proves the existing wiring, not the proposed isolation targets. [Phase log] and [image digest evidence] remain private scratch.

[bucket/IAM inventory]: ../tmp/cw-isolation-live-inventory.json
[namespace inventory]: ../tmp/cw-isolation-namespace-inventory.json
[preview]: ../tmp/cw-isolation-preview.log
[saved plan]: ../tmp/cw-isolation-preview-plan.json
[Phase log]: ../tmp/cw-scan-20261012-12-phases.log
[image digest evidence]: ../tmp/cw-scan-20261012-12-image.log
[verified inventory]: ../tmp/cw-isolation-inventory-20261012-verified/summary.json
[copy cost estimate]: ../tmp/cw-isolation-copy-cost.json
[build identity]: ../tmp/cw-isolation-build-publisher.json
[build IAM]: ../tmp/cw-isolation-build-publisher-iam.json
[builder permissions]: ../tmp/cw-isolation-build-role-permissions.json
[CF permission evidence]: ../tmp/cw-isolation-cf-permission-scopes.json
[CW R2 policy]: ../tmp/cw-isolation-r2-token-policy.json
[the refreshed foundation preview]: ../tmp/cw-isolation-foundation-preview.log
[foundation plan]: ../tmp/cw-isolation-foundation-plan.json
[foundation apply log]: ../tmp/cw-isolation-foundation-apply.log
[post-apply live preview]: ../tmp/cw-isolation-foundation-post-apply-preview.log
[migration inventory]: ../tmp/cw-isolation-inventory-migration-prep/summary.json
[copy plan]: ../tmp/cw-isolation-copy-plan/summary.json
[the baseline CF preview]: ../tmp/cw-isolation-cf-iac-default-preview.log
[the additive CF preview]: ../tmp/cw-isolation-cf-iac-preview.log
[The CF plan]: ../tmp/cw-isolation-cf-plan.json
[The approved CF plan]: ../tmp/cw-isolation-cf-approved-plan.json
[the CF post-apply preview]: ../tmp/cw-isolation-cf-post-apply-preview.log
[Build log]: ../tmp/cw-isolation-image-build.log
[Batch smoke]: ../tmp/cw-isolation-batch-smoke-final-task-logs.json
[October 13 pre-drain inventory]: ../tmp/cw-isolation-inventory-before-drain/summary.json
[Scratch preservation audit]: ../tmp/cw-isolation-scratch-before-drain/summary.json
[Candidate cutover plan]: ../tmp/cw-isolation-cutover-candidate-plan.json
[Drained inventory]: ../tmp/cw-isolation-inventory-drained/summary.json
[Payload journal]: ../tmp/cw-isolation-payload-copy/summary.json
[Scratch journal]: ../tmp/cw-isolation-scratch-copy/summary.json
[Published controls]: ../tmp/cw-isolation-controls-published/_SUCCESS.json
[Preview live checks]: ../tmp/cw-isolation-preview-live-checks-normalized/_SUCCESS.json
[Production live checks]: ../tmp/cw-isolation-production-live-checks/_SUCCESS.json
[Reviewed cutover plan]: ../tmp/cw-isolation-cutover-reviewed-plan.json
[Cutover apply]: ../tmp/cw-isolation-cutover-apply.log
[Production cold check]: ../tmp/cw-isolation-production-cold-checks/_SUCCESS.json
[Post-apply preview]: ../tmp/cw-isolation-cutover-post-apply-preview.log
[Meta task logs]: ../tmp/cw-isolation-meta-validation-final-task-logs.json
[Production meta view]: https://cw-s3.oa.dev/meta?d=2610131145
[First isolated scan]: https://cw-s3.oa.dev/scans/job-f4527d8b-f524-25e28fac-9701-4d4800
[Scheduled scan watch]: ../tmp/cw-isolation-first-scheduled-scan-watch.log
[Final Batch state]: ../tmp/cw-isolation-first-scheduled-scan-final-job.json
[Final phase log]: ../tmp/cw-isolation-first-scheduled-scan-final-phases.log
[New scan live checks]: ../tmp/cw-isolation-first-scan-live-checks/_SUCCESS.json
[New scan view]: https://cw-s3.oa.dev/?d=2610131201
[Exact warm-request retry]: ../tmp/cw-isolation-first-scan-warm-retest.json
[Cold serving probe]: ../tmp/cw-isolation-first-scan-serving-probe.json
[Static compaction watch]: ../tmp/cw-isolation-first-static-compaction-watch.log
[Static compaction result]: ../tmp/cw-isolation-first-static-compaction-final.log
[Revocation apply]: ../tmp/cw-isolation-revoke-apply.log
[Revocation refresh]: ../tmp/cw-isolation-revoke-post-apply-preview.log
[Post-revocation serving]: ../tmp/cw-isolation-post-revoke-serving/_SUCCESS.json
[Post-revocation smoke]: ../tmp/cw-isolation-post-revoke-watch.log
[Post-revocation task logs]: ../tmp/cw-isolation-post-revoke-task-logs.log
