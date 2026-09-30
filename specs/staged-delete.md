# Staged delete: the `chat-approve` cloud path, upstreamed generic

**From:** the mgu gcs "delete not mark" pivot (`specs/gcs-toward-union.md` §5 seam 1 + `specs/sweep-plan-union.md`, mgu-side), routed here as roadmap item 3 (`specs/mgu-cp-2026-09-16.md`). Cross-repo peer, so **generalize-and-upstream** (`/cp` `handoff: spec`): disk-tree holds the reusable engine, each deployment keeps its policy; gcs eventually imports it.

**Goal:** a public demo against Ryan's **S3 + R2**, in the repo — you log in and do admin dispatches. It doubles as the forcing function to make the ported pieces appropriately generic (S3/R2/GCS, not gcs-only).

## The model (the pivot)

Opt-out mark/sweep + deadline → opt-in **delete**. Three nouns collapse to one user-facing one:

- **stage** (the gesture) — a per-row trash-can (dt already has it in local mode; shift-range; treemap meta-drag later). Stages exactly what it names. *Nothing is deleted by inaction — there is no deadline.*
- **[approve]** — the per-deploy authority gate; absent in local mode.
- **run** — the executor deletes, recoverably.

Lifecycle (cloud): `staged → pending-approval → running → done, recoverable until X` — no fake-instant checkmark; the run is async. Local collapses to `trash → confirm → delete inline`.

Per-deploy policy is **config, not forked code** (`Store.*` in mgu; a `delete:` block here):

| policy | values | meaning |
|---|---|---|
| `approval` | `local-confirm` \| `chat-approve` | single-user confirm dialog vs stage→chat→admin→dispatch |
| `chat` | `slack` \| `discord` \| none | where a staged path is announced (reuses `disk_tree.notify`) |
| `undo` | `soft-delete` \| `versioning` \| `none` | what makes one-click delete safe; set by the store adapter's *offer* |
| `eligibility` | `owner-slice` \| `any` | an *optional* auto-approve shortcut, **not** a safety gate; default: approve every real delete |

## Two orthogonal axes (target model)

CP1–4 built **one cell**: approval = *staged*, executor = *laptop CLI drainer over direct D1*. The design generalizes along two independent axes — **approval** (the trust/UX gate) and **execution** (how a delete actually runs, by scope). They don't hardcode by path type; they're config, with an optional per-user layer.

**Axis 1 — approval.** Per-deployment `delete.approval`:
- `sync` — delete inline (confirm dialog), no queue. (`local-confirm` today.)
- `staged` — always the admin queue. (`chat-approve` today — gcs / less-trusted Stanford users; the control is **not offered**.)
- `user-choice` — offer a **sticky per-user setting in the logged-in chip** (sync ↔ staged), persisted per identity; the deployment sets the default and may cap/lock it. A locked deployment hides the control entirely, so trust is enforced server-side, not by a hideable client toggle.

**Axis 2 — executor**, chosen by deletion scope (a per-user/-deploy **threshold**): small → **CFN**, large → **Batch**. Backend-specific, IaC-provisioned:

| backend | small (CFN) | large (Batch) |
|---|---|---|
| R2 | **Worker + R2 binding** — same-account, *no external creds, no laptop drainer* | Worker queue / Durable-Object chain |
| S3 | Lambda (IAM role) | AWS Batch |
| GCS | Cloud Function | GCP Batch (what gcs runs today) |

The R2 row is the key simplification: for Ryan's own R2 in his CF account, a bound Worker deletes directly, so **small R2 deletes run at the edge as a CFN** — the CP4 laptop drainer is then only the fallback for arbitrary/cross-account buckets and large (Batch) jobs. The executor is an interface (`plan → run`) the approval axis dispatches into; `sync` calls it inline, `staged` calls it from the queue.

**IaC (roadmap item 2)** provisions the CFN + Batch resources per backend (S3/R2/GCS) so a deployment stands the executors up seamlessly — the forcing function for making the ported pieces genuinely generic.

The four axes above stay; `approval` gains the `sync | staged | user-choice` framing and `delete.executor` (`cfn | batch | drainer`, `threshold`) is the new axis.

## What gcs built — generic vs specific

Generic (upstream): the **plan object** (`plans`/`plan_items`, the shared cw+gcs schema — a staged set auto-populated by trash gestures), the run records (`deletion_runs`/`deletion_bands`), the `/api/plans` CRUD + `/stage` shape, the generic `/api/db` registry-CRUD, the `auth.ts` scope/gate, `edgeCache.ts`, and the **stage → approve → dispatch → runs** UX.

gcs-specific (abstract behind an interface): GCP Batch dispatch (`gcp.ts`, `api/sweep/dispatch.ts`), the `sweep_exec.py` executor (re-list + generation-match + ≥7d soft-delete guard + drift), the storage-class axis, the attribution/ownership gate (`sweep_approvals`, `attr_index`, `classify_dir`), KLC checkpoint-keeping, and every `gs://marin-<bucket>/` prefix + hardcoded bucket.

## What disk-tree already has (so we don't rebuild it)

- **Auth is not greenfield.** dt ships the *same* `@open-athena/auth` stack gcs uses, live on `disk-tree.pages.dev`: D1 `DB` binding, `view`/`admin` scopes, `ALLOWED_EMAILS`→admin, `hasScope(auth,'admin')`, an `AccessPage.tsx` admin dashboard, migrations 0001–0007 (`grants`/`access_log`/`access_requests`). So "mirror what gcs uses" for admin auth **falls out for free** — reuse `gateFor`/`gateApi` + the `admin` scope; `view` to stage, `admin` to dispatch.
- **The executor already exists locally.** `S3Backend.delete()` = `aws s3 rm --recursive` (R2 via endpoint+profile), `local`/`ssh` deletes too, all wired through `backend_for()` with `buckets.yml` creds. `POST /api/delete` (Flask) does an immediate delete today.
- **The edge is read-only.** Every non-GET `/api/*` 501s (`ui/functions/api/[[path]].ts`); the edge R2 binding (`SCANS`) reaches only the dt scans bucket, not arbitrary user buckets. So the *edge cannot delete* — the executor must stay server-side (laptop/worker with `buckets.yml` creds), and the edge only records intent + approval.

That shapes the split: **edge stages + approves (D1); a server-side dispatcher drains approvals and runs `backend.delete()`.** Local-first proves the whole mechanism before any of the edge/D1/UI exists.

## disk-tree design

**Data model** — mirror gcs's D1 tables in dt's SQLite (so the later D1 layer is a direct mirror), on `Base` (auto-created by `create_all`):

- `Plan` — `id, name, note, state('open'|'closed'), created_by, created_ts, closed_ts`.
- `PlanItem` — `(plan_id, uri)` PK, `note, added_by, added_ts`. The staged set.
- `DeletionRun` — `run_id, plan_id, mode('dry'|'real'), actor, started_ts, finished_ts, deleted_bytes, deleted_objects, skipped_gone, undo_state('none'|'partial'|'full'|'expired'), undo_deadline`.
- `DeletionBand` — `(run_id, uri)` PK, `bytes, objects, deleted, gone` — the per-item result of a run.

**Executor** — generic, over the existing backend seam: for each plan item, size it from the freshest covering scan, then `backend.delete(uri)` (dry: skip, report; real: delete + record a band). Undo is the store's own affordance (S3/R2 versioning → delete-markers; GCS soft-delete window), recorded on the run, not re-implemented — dt relies on the object store for recoverability rather than gcs's PB-scale generation-match walk.

**Policy** — a `delete:` block per bucket (or `defaults`) in `buckets.yml`: `approval`, `chat`, `undo`, `eligibility`. Local dt (no block) = `local-confirm`. On stage under `chat-approve`, announce via `disk_tree.notify`.

## Checkpoints

1. **Local data model + CLI (this stage). Done.** `Plan`/`PlanItem`/`DeletionRun`/`DeletionBand` SQLite models (`sqla/deletion.py`, mirroring gcs's D1 shape; auto-created) + the engine `disk_tree/staged.py` (`stage`/`unstage`/`open_plan`/`plan_by_ref`/`dispatch`, with `delete_fn`/`size_fn` injected so it's backend- and DB-init-free) + the CLI `disk-tree stage URI…` / `staged` / `unstage URI…` / `dispatch [PLAN] [-f]`. The executor runs over the existing `backend_for(uri).delete()`, dry by default; a run + per-URI bands recorded; sizing from the freshest covering scan. Tested: engine units (in-proc session + fake backend) + a real CLI end-to-end (index → stage → dry → real delete of local temp files). *The dispatcher is the admin — no approval gate yet.* (`run_id` carries a random suffix — a plan takes many dry runs + one real, so a second-resolution timestamp alone collides.)
2. **Edge write layer. Done.** The four tables mirrored into D1 (`ui/migrations/0008_staged_delete.sql`, same DB as the auth stack; timestamps epoch-seconds per the `grants`/`access_log` convention). The D1 data layer `ui/cfn/staged.ts` (`openPlan`/`stage`/`unstage`/`planByRef`/`planItems`/`listOpenPlans`/`listRuns`/`enqueueDispatch`) mirrors the CP1 Python engine. Routes in `ui/cfn/stagedRoutes.ts` behind thin Functions: `POST /api/plans/stage`, `POST /api/plans/unstage` (`view`), `POST /api/dispatch` (`admin`), `GET /api/staged` (the staged set + runs feed). Gating reuses `@open-athena/auth`: the `/api/*` middleware enforces `view`; the handlers re-derive the identity (`authFor`/`actorLabel`) to record an actor, and `dispatch` additionally checks `ADMIN_SCOPE`. A `stageDelete` capability (on where gated, off on the open demo and on immediate-`delete` local servers' peer). **The edge never deletes** — the executor can't reach arbitrary user buckets — so `dispatch` *enqueues*: it writes a real `deletion_run` with `finished_ts IS NULL` (pending) and closes the plan; the CP4 server-side drainer sizes each item, deletes, and lands the bands. Tested (`ui/cfn/tests/staged.test.ts`, 12 specs) over an in-memory `node:sqlite` D1 (`fakeD1.ts`, all migrations applied) driving the *real* gate: `view` stages, only `admin` dispatches, open-mode 501s.
3. **`/staged` React page + trash-can rewire. Done.** `ScanDetails`'s trash-can *stages* (`stageUris`) instead of deleting immediately where `canStage = supportsStage(routeType) && caps.stageDelete` — cloud buckets (`s3`/`r2`, `supportsStage`) that a server-side executor deletes; `file`/`ssh` keep the immediate `delete`. The per-row + bulk affordance turns amber and its tooltip/label read "Stage"; a success notice links to `/staged`. The `/staged` page (`StagedPage.tsx`, `AccessPage` patterns) lists open plans + items with the runs feed; dispatch is gated (`isAdmin`, or the ungated local server) — the two-step confirm calls `dispatchPlan`. A Header nav link shows wherever `caps.stageDelete`. **Both servers implement the HTTP surface:** the edge (CP2) enqueues; the Flask peer (`server.py` `/api/staged`, `/api/plans/stage`, `/api/plans/unstage`, `/api/dispatch`) deletes inline (local-confirm) over the CP1 engine via `staged_backend.py` (a standalone SQLAlchemy engine — *not* flask-sqlalchemy's `init()`, whose app-context push corrupts a request context). `stageDelete` added to `ALL_CAPABILITIES` + the Flask capabilities. CIC-verified: the `/staged` page renders plans/items/runs, stages/unstages/dry-dispatches live; the nav link resolves.
4. **Server-side dispatcher + chat. Done.** `disk-tree dispatch --serve` (`cli/staged.py` `_serve`) drains the edge-enqueued runs. It reaches D1 **directly** via the `CLOUDFLARE_API_TOKEN` the laptop already holds (`disk_tree/d1.py` `D1Client`, the D1 REST API) — no bespoke authenticated Functions endpoint, no service token. `disk_tree/drain.py` (`pending_runs`/`execute_run`/`drain_once`, DB duck-typed + `size_fn`/`delete_fn` injected like CP1) sizes each staged URI from the local scan DB and deletes it through `backend_for`, writes a per-URI band + the run's totals/`finished_ts` back to D1; a single URI's failure is recorded, not fatal. Exp-backoff poll loop (`-i` base, ×5 idle; `-o` once; Ctrl-C clean). Chat: a deployment-wide `delete:` block in buckets.yml (`chat: slack|discord|none`, `undo`, `database_id`, per-platform secret env names) → `notify/announce.py` `make_announcer` posts a per-run message (`disk_tree.notify`, the `notify`/thrds extra). Tested (`tests/test_drain.py`, 8 specs) against an in-memory fake D1 + `D1Client` over a stubbed `urlopen`; the client verified live read-only against the real D1 (tables present, no pending runs). *The demo drainer is the laptop poller (open question resolved).*
5. **Pluggable undo. Done.** `Backend.restore(url) -> int` (spec) — the store's undo, default "no undo" (a plain delete is permanent). `S3Backend.restore` (S3 + R2) removes the latest delete-markers under the prefix on a versioned bucket (`aws s3api list-object-versions` → `delete-object --version-id`), each revealing the object's prior version; 0 if unversioned. The engine `staged.undo_run(session, run, restore_fn)` restores a run's deleted bands (`restorable_bands` = `deleted == 1`), records `undo_state` (`full`/`partial`), injected `restore_fn` = `staged_backend.restore_fn` = `backend_for(uri).restore`. CLI `disk-tree undo RUN_ID [-f]` — dry (report restorable scope) by default, `-f` restores. Tested (`tests/test_staged.py`): engine full/partial + dry-run exclusion + a CLI dry-undo E2E. *Local/ssh have no undo (restore raises); versioning has no deadline, so `undo_deadline` stays meaningful only for a retention-window store.*
6. **Configurable approval + user-sticky method. Done.** `deleteApproval` capability (`sync | staged | user-choice`): Flask from `DISK_TREE_DELETE_APPROVAL` / buckets.yml `delete.approval` (default `sync` — a credentialed server just deletes); the edge is `staged` (no inline executor for arbitrary buckets). `ui/src/hooks/useDeleteMethod.ts` (`resolveDeleteMethod` pure + a `useSyncExternalStore` sticky `localStorage` preference) yields the effective method; the Header shows a Sync/Staged toggle **only** when `user-choice` (locked deployments hide it, so trust is server-side). `ScanDetails`'s trash gesture now branches on the *method*, not the scheme: `supportsDelete` extended to `s3`/`r2` (all delete-capable backends), `canSync`/`staging` = `deletable && caps.{delete,stageDelete} && method === {sync,staged}`. This folds in the "laptop sync-deletes cloud" fix (sync mode deletes cloud inline) and the `/staged` nav now hides on an always-`sync` deployment. `deleteApproval` added to `ALL_CAPABILITIES` + both capability endpoints. CIC-verified: the toggle renders under `user-choice`; sync ⇒ cloud rows "Delete" (inline), the sticky `staged` preference ⇒ "Stage".
7. **Pluggable executor + scope threshold — R2 CFN cell. Done.** The edge `POST /api/dispatch` keeps the staged UX but its *executor* is now CFN-when-possible (`ui/cfn/r2exec.ts`): if every staged URI resolves to a bound same-account R2 bucket (`R2_<bucket>`, sanitized) *and* the cumulative scope is under `DELETE_THRESHOLD` (default 1000 objects), it lists + bulk-`delete`s inline through the R2 binding and records a *finished* run (`recordInlineRun`, `state: done`) — no drainer. Any unbound bucket, non-R2 URI, or over-threshold scope falls back to `enqueueDispatch` (`state: enqueued`, CP4 drainer). Dormant until buckets are bound in `wrangler.toml` (documented template + `DELETE_THRESHOLD` var), so it degrades cleanly. Tested (`ui/cfn/tests/r2exec.test.ts` + 2 CFN cases in `staged.test.ts`, over an in-memory R2 + fake D1): binding resolution/sanitizing, object+prefix key collection, the threshold cutoff, inline-delete vs enqueue-fallback. *Approval axis unchanged — CFN is an executor optimization behind `staged`.* Next cells: S3 Lambda / GCS Cloud Function (small) + AWS/GCP Batch (large).
8. **IaC for CFN + Batch (roadmap item 2). Started.** The verified backbone is the generator `disk_tree/iac.py` + `disk-tree iac` (`r2-bindings` → the `[[r2_buckets]]` wrangler.toml blocks that activate the CP7 CFN for each configured R2 bucket; `config` → the `CfnDashboard` Pulumi config) — deployment config derived from `buckets.yml`, one source of truth (`tests/test_iac.py`, 7 specs). The declarative layer is the `iac/` Pulumi `CfnDashboard` component (the CF reference deploy — Pages project + bindings/vars + D1 + R2 data & CP7 executor buckets; store-parameterized; `batch` arg = the S3/GCS large-scope extension point), per marin-gcs-usage `specs/cf-iac.md`. The component is typechecked + applied where the `@pulumi` SDK + CF creds live (OA ops), not in this repo. **AWS Batch cell — done:** the drainer picks executor by scope — a run over `delete.batch.threshold` is handed to AWS Batch (`disk_tree/batch.py` `submit_delete_job`) instead of deleting inline; a nullable `deletion_runs.batch_job` (D1 migration `0009` + the SQLite/TS mirror) marks it "running in Batch" so `pending_runs` (now `finished_ts IS NULL AND batch_job IS NULL`) skips it until the job's container (`iac/aws/delete-job/`) deletes + writes the result back to D1. IaC: `iac/aws/` Terraform (Fargate compute env + queue + job def + bucket-scoped IAM) + the container; `disk-tree iac aws-batch` emits its tfvars from buckets.yml. Tested (`tests/test_drain.py` submit path + payload; `tests/test_iac.py` tfvars). Remaining: the GCP Batch resources (mirror the AWS cell) + the S3 Lambda / GCS Cloud Function *small* cells (the drainer's inline `aws s3 rm` already covers S3-small).

## Open questions

- **Edge → arbitrary-bucket delete.** *Resolved (CP4):* the edge enqueues; a laptop/worker `disk-tree dispatch --serve` poller drains D1 directly (its `CLOUDFLARE_API_TOKEN` reads/writes D1 over the REST API) and deletes with `buckets.yml` creds. For the demo, the poller is Ryan's laptop.
- **Eligibility.** dt has no attribution/owner ledger (that's gcs's `actions` WAL). `eligibility: any` (approve everything) is the dt default; `owner-slice` is deferred until/unless dt grows an owner axis.
- **Undo parity.** S3/R2 give versioning (delete-markers) only — no soft-delete window. The run's `undo_deadline` is meaningful only where the store has a retention window; for plain versioning it's "restore the marker" with no deadline.
