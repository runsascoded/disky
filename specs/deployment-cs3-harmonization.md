# Deployment and CS3 harmonization

Status: active, 2026-11-09. Scope: GCS, CW, HF and OA R2, including each meta store. Root `cloud` owns shared source; deployment owners own configuration, verification, infrastructure applies and cutovers. Louisa's permissions are outside this work. The [operator runbook] describes the four release lanes. This spec records the missing parity and the implementation order, independently of the [index rethink].

## Acceptance

An operator follows the same release protocol for every deployment: validate configuration and committed source; run required checks on a dev node/CI; publish and verify a preview; verify the exact candidate scan image; apply only required schema recipes; promote deliberately; verify production and retain rollback identities. Site, image, schema, infrastructure and data-pointer changes remain distinct actions. Store API, region, source profile and resource names are legitimate intrinsic differences.

Every deployment has a published, reproducible source baseline, correct production/preview status reporting and explicit preview binding behavior. A successful Pages upload does not prove that source was pushed, an image was promoted or a recipe was applied. Preview currently shares production D1/R2; it cannot be described as a data sandbox. Image builds should target explicit candidates rather than implicitly moving live tags. R2's local Pulumi state needs an OA-controlled recoverable backend/backup before it has the same recovery story as the other three; preparing that migration does not authorize applying it.

Every primary and meta store needs independent interval/static/age histories, verified source coverage, native retirement of redundant per-scan representations, lineage-aware generation GC, and accountable temporary/retry storage. Build and serving costs must both be measured; retiring a full sort after building it saves retained bytes but not construction work. A store-specific feature exception is documented with its limitation and evidence, not hidden by an absent step.

Strict CS3 means bounded physical storage under a bounded workload, retained-history and recovery policy. O(changes) histories avoid full-copy duplication but can still grow forever. Exact arbitrary as-of history cannot be constant-size under unbounded distinct changes. The first implementation target is bounded redundant copies, superseded generations and operational intermediates, preserving currently promised historical answers. No finite historical horizon or destructive thinning policy is chosen implicitly here. Record any intentionally unbounded history/ledger separately; a future history policy requires its own semantic decision and verification.

## Published-source audit

Inspected 2026-11-10 03:22 UTC: `o/gcs=85c7dc340`, `o/cw=4e8d412dc`, `o/hf=9230462fa`, `o/r2=51b0018f6`. These are source settings and owner handoffs, not a new bucket inventory or proof of the running cron's revision. Exact source copies and the full audit are in root `tmp/cs3-audit/`; durable findings follow.

| Deployment | Primary gaps | Meta gaps | Operational/release gaps |
| --- | --- | --- | --- |
| GCS | No primary age history or explicit store-GC step; full input sorts still built before keep-2 retirement | Daily profile omits interval history and path/raw/cache retirement; independent age history is present | Work TTL only; cache/reports lack equivalent ownership-aware retention; interval auto-compaction disabled |
| CW | Published profile lacks age history/store GC and retains redundant grouped over-time data | No interval/age history or native retirement in published daily profile | Data bucket has no work/cache/report lifecycle; newer GC/thinning source remains in owner worktree and must be sealed before adoption |
| HF | Best existing O(changes) baseline: independent histories and executing keep-2 retirement/GC | Same baseline as primary | Work/cache blanket 14-day TTL can expire resume evidence; small ledgers and all retained history still grow |
| OA R2 | Path retirement keep-1 exists; raw/cache coverage omitted; age retirement explicitly non-executing | Histories exist, but path/raw/cache retirement omitted and age retirement non-executing | Work retention unimplemented; collector frozen inputs/aggregates remain; local IaC state differs from other deployments |

GCS/CW meta are the largest straightforward parity misses. Shared secondary-store and age engines are already on `cloud` (`specs/done/cs3-history-handoff.md`); rollout is missing, not an engine rewrite. GCS's meta-history repair profile does not make its daily meta profile interval-enabled. R2's held exact deletion proposal is not an executed cleanup.

## Source adoption in progress

The isolated `w/harmonization` branch is based on `cloud` `917560a2`. Changes below are local source adoption pending combined validation; no release or cleanup is implied.

| Shared change | Source handoff | Acceptance |
| --- | --- | --- |
| Pages status pagination | HF `fdb5b46f3` | 25-item API pages, exact branch/environment selection, bounded 1,000-record search; includes stronger regression |
| Nominal run threads | HF `f9cf9d97` | Final status updates; ambiguous pending Slack sends fail closed instead of risking duplicates |
| Empty reviewed scopes | GCS `e4ed3d0b` | Complete no-op reviews, preserve newer versions; adapted without deleted `API.md` or the unrelated `StagedPage` totals feature |
| Batch reconciliation | GCS `99c3e5b0` | Resolve successful Batch names to recorded deletion run IDs before settling plans |
| Retirement identity proofs | R2 `a41af3fa`, `1938f9b0`, `21d8467f`, `bf539876` | Raw string versions need string parity; empty shards do not imply identities; churn/object-version lineage retains proofs |
| Exact reviewed delete sets | R2 `8e9c3470` | Optional review gates compare exact root/key/byte sets and explicit D1 intent before native deletion; existing native safety checks remain |
| Opaque version inputs | R2 `dc1c3962` | `path-index -V` carries ETags/exact decimal generations, NULL on directories, including sharded roots; unrelated R2 checkpoint test omitted |
| Per-head work ownership | New additive source | Full workdir trace plus immutable, store/run/attempt-specific receipt; no deletion implementation |

CW's active GC/thinning changes overlap retirement, compaction, publishing and scan-step code. Request coherent shared commits and validation from its owner; do not merge a whole deployment branch or implement a third conflicting GC. The R2 guard stack above should be retained when reconciling that work.

## Implementation sequence

1. Finish shared-source adoption and targeted Python/site checks. Keep root's unrelated staged changes intact. Publish exact handoff commits and test evidence to owners; distinguish local fixes, branch adoption and deployed fixes.
2. Add work ownership evidence before implementing Nextflow cleanup: deployment/store/actual run/head-attempt identity, exact configured task root, full workdirs, cached/shared references, trace digest and explicit missing/truncated evidence. Immutable local/GCS writes refuse a different receipt at the same key. Existing report links remain unchanged. A receipt is an inventory prerequisite, not output parity or permission to delete.
3. Bring GCS/CW meta histories and CW/GCS age histories to the shared baseline on candidate generations. Verify every promised scan against its existing reader and check fresh append/restart behavior. Only then produce exact native retirement manifests; report retained and held bytes by representation and root. No cleanup from missing or mismatched proofs.
4. Reconcile published generation GC with CW's validated source, protect all primary/meta/static rollback roots and active reader/writer references, and prove continuation from the retained generation. R2 can then re-plan its already-held proposal with the shared guard stack. Configuration changes that enable deletion require the exact review and deployment owner cutover.
5. Implement a read-only completion-aware work/cache/report planner over complete ownership receipts. Protect active/failed resumable runs, shared cached workdirs, explicit holds and output proof. Define a resume fence and recovery window before any executor. Do not copy HF's blanket TTL as an ownership proof.
6. Account for the remaining small growth: scan/phase/DAG records, scan rosters, verification markers, tombstones, access collector frozen maps and redundant over-time files. Give each an explicit retained-history/recovery exception or a bounded summary/archive policy. Do not prune arbitrary as-of history under the label of scratch cleanup.
7. Make the common release story reproducible: guarded `site/deploy`/`cf-status` on every published branch, clean source/image stamps, candidate digest records, required recipe inventory, recoverable IaC state and documented rollback. Preserve intrinsic source/region/resource differences rather than forcing unsafe identical settings.

## Validation and current blockers

The earlier shared deploy/status suite passed 25 cases on `mgu`. The census separately passed 25 cases and mypy there. Ryan refreshed `oa-ci-dev` on November 10, unblocking the existing dev node. The additive ownership checkpoint is `9f1236959`; [ownership contract] describes its exact evidence and limits. Runtime validation corrected two invalid test fixtures (local executor with a cloud work root, and a fake GCS bucket omitting its name); production receipt code required no change. Validation also found and fixed `scripts/remote-test` suppressing pytest's empty-suite exit after another suite passed. Missing test paths had caused the initial cloud run to collect nothing; that run is not counted as acceptance.

Verified on `mgu` on November 10: 23 engine deploy/status cases; 439 broader cloud cases; 68 ownership/head/actual Nextflow cases (overlapping the broader set); four new empty-suite aggregation regressions; and 129 affected site cases. The final site TypeScript checks and production build pass. Broad cloud source snapshot `2dc4d6e2d1e0` had the ownership-test syntax error separately corrected and accepted in focused snapshot `49b48d605ec4`; wrapper regressions passed in `6123dc1a56d8`. Site snapshot `440c7aec65ed` passed its 129 cases and build. Logs are in `wt/harmonize/tmp/remote-test-20261110T075539.log`, `remote-test-20261110T075717.log`, `remote-test-20261110T080102.log`, `remote-test-20261110T075548.log` and `remote-harmonize-site-build.log`. These checks certify shared-source behavior, not deployment adoption, live cleanup or a production smoke.

Resume remote checks from that worktree with `REMOTE_TEST_CONFIG=/dev/null REMOTE_TEST_HOST=mgu REMOTE_TEST_INSTANCE=i-033c60f359aca85e8 REMOTE_TEST_AWS_PROFILE=oa-ci-dev scripts/remote-test -n 4 -d disky-test/harmonize-python`, passing the changed Python suites plus scan-run wrapper/recording, native generation-GC and deploy dry-run regressions. Run the four changed staged/deletion site suites separately with `--site -d disky-test/harmonize-site`. Nextflow execution checks additionally need a working Nextflow installation. Use the existing node; no new compute deployment is required. Confirm its current address through the helper rather than trusting the stale SSH alias DNS.

Use targeted suites for nominal threads, scan finalization, ownership receipts, reviewed sweeps, Batch reconciliation, version-aware path inputs, interval parity, native retirement/GC and deploy/status helpers. Exercise affected site routes/outcomes in Vitest and typecheck the final tree. Any visible changed UI needs browser verification; local source testing alone is not a production smoke. Record exact source/test IDs and each owner's deployed revision separately.

[operator runbook]: oa-deployment-handoff.md
[index rethink]: fleet-index-research.md
[ownership contract]: ../docs/nextflow-ownership.md
