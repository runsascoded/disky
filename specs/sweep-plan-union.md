# The unified deletion model (opt-in trash) — implementation

The build plan for the decided seams (`specs/done/gcs-toward-union.md` §5). Both
branches build to this so they converge by construction; DT upstreams the result
into `disk_tree` (its `specs/mgu-cp-2026-09-16.md` roadmap item 3) once it runs on
both. Written 2026-09-17; revised the same day when seam 1 flipped from plan-first
to **opt-in trash** — a faster `rm` with better situational awareness, scope cut
everywhere it allows.

## The model

Deletion is always an explicit per-path act. There is no deadline and no
"unmarked is swept" — nothing is deleted as a consequence of inaction. The user
gesture is a **trash-can** (per table row, à la disk_tree local mode; shift-click
a range; treemap meta-drag later), and it stages exactly what it names.

Three nouns collapse to one user-facing one:

- **trash (stage)** — the gesture. Adds the path to the staged set.
- **[approve]** — per-deploy authority gate (below); absent in local mode.
- **run** — the executor (gcs's `sweep manifest`/`execute`) deletes, recoverably.

The **staged set is the plan object** (`plans`/`plan_items`), auto-populated by
trash gestures instead of hand-curated. `deletion_runs`/`deletion_bands` record
each run. None of that is a separate step the user assembles.

### Per-deploy policy (config, not forked code)

- **Authority — `Store.approval`:**
  - `'local-confirm'` (disk_tree local, single-user): trash → confirm dialog →
    delete. No chat, no staging page.
  - `'chat-approve'` (gcs, cw): trash *stages* the path and posts to the deploy's
    channel (`Store.chat: 'slack' | 'discord'`); an admin whitelists/approves;
    approval fires the run. A `/staged` page lets admins multi-select staged
    paths and dispatch. Non-admin viewers see `/staged` + runs feed **read-only**.
- **Eligibility auto-approve (optional) — `Store.sweepEligibility: 'owner-slice'
  | 'any'`.** Demoted from a core gate to an auto-approve shortcut: under
  `'owner-slice'` an admin's trash of a path they own can skip the chat
  round-trip; everything else needs approval. `'any'` (cw, no owner data) never
  auto-approves on ownership. Default: approve every real delete.
- **Undo — `Store.undo: 'soft-delete' | 'versioning' | 'none'`,** constrained by
  the adapter's offer: GCS offers soft-delete (default window) **and** versioning;
  S3/CAIOS offers versioning only. `undo`/`purge` endpoints exist only where a
  permanent-delete cloud needs them; GCS defaults to its soft-delete window and
  has neither. The undo window is what makes one-click delete safe at PB scale.

Deleted by the flip: `Store.unmarked`, the deadline, the `/sweep`
assemble-a-plan page, the `sweep` keep-kind, the auto-seeded-default-plan design.

## Checkpoints (each buildable: tsc + vitest + py suite green; destructive-path ones CIC'd)

1. **[done] Staged-set schema.** `plans`, `plan_items`, `deletion_runs.plan_id`
   (nullable), `deletion_runs.purge_state`. Migrations 0024/0025, cw's shape
   verbatim, `IF NOT EXISTS`. Apply to remote D1 is user-gated.
2. **[done] Staged-set CRUD (`/api/plans`).** Ported cw's `api/plans/[[path]].ts`
   + `_lib/plans.ts`, gcs-adapted: `gs://` prefixes, gcs auth
   (`requireViewer`/`requireAdmin`), `audit()` → gcs's `admin_edits`. gcs plans
   MAY span buckets (executor takes multiple `-b`); no `PlanSpansBuckets` refusal.
   The user-facing framing becomes "staged," but the API shape stands. (Commits
   `eeeda8b`, `87aad75`.)
3. **Executor consumes the staged set.** `sweep manifest` gains `--plan <id>`
   (reads `plan_items` for the delete set) alongside today's ledger-sourced path;
   the dispatch bridge (`api/sweep/dispatch.ts`) takes `{ plan_id, mode, date,
   buckets? }` and records `deletion_runs.plan_id`. No behavior change for gcs's
   existing flow when a plan isn't supplied (keep the implicit path until the UI
   moves over). **Destructive path — dry-run only in tests; no real dispatch.**
4. **Trash gesture + authority gate (replaces the /sweep page).** The row/treemap
   trash-can stages a path; shift-click stages a range. Under `'local-confirm'`,
   confirm → delete inline. Under `'chat-approve'`, the gesture stages + posts to
   `Store.chat` and lands on `/staged`, where admins multi-select and dispatch;
   non-admins get `/staged` + runs read-only. Confirm/stage UI must show
   bytes/objects/est-cost and the recovery deadline before any real delete, and
   render honest state (staged → pending-approval → running → done, recoverable
   until X) — no fake-instant checkmark, since the run is an async Batch job.
   **CIC** the trash flow, `/staged` (admin vs RO), and a dispatch dry-run.
5. **Pluggable undo.** `undo`/`purge` behind `Store.undo`; the GCS adapter's
   soft-delete restore vs the CAIOS adapter's version-restore + purge. gcs keeps
   its soft-delete window as the net (no new endpoints); cw's `undo.ts`/`purge.ts`
   become the CAIOS adapter.
6. **Scope-cut demolition (largest blast radius; do last, deliberately).** Retire
   the deadline, `Store.unmarked`, the `sweep` keep-kind, and the keep axis as a
   protective mark. Keep the **owner axis** (attribution — the
   situational-awareness substrate). `keep_last_ckpt` survives only as a
   trash-time modifier ("trash all but the newest under here"), computed at stage
   time, not a standing mark. Touches `/marks`, `resolve`, `todo`, the ledger fold
   — sequence it as its own reviewed step, not folded into 3–5.

## Coordination

- **cw-s3 owns seam 2** (adopt the `actions` owner ledger, retire
  `marks`/`mark_log`) and **retiring its `plan-sweep` executor** in favor of
  gcs's. gcs owns the executor rewire (3), the trash/authority UI (4), undo (5),
  and the demolition (6). Both land on the union with the checkpoint protocol.
- **DT** upstreams nothing until checkpoints 3–4 run on both branches; then the
  owner ledger + staged-set + executor + pluggable trash gate is the `disk_tree`
  cloud engine (roadmap item 3), with local mode as the `'local-confirm'` case.
  Seam 4 (digest profiles) is DT's comms item 1 and is independent.
- **D1 writes** (`migrations apply`) are gated to the user throughout.

## Non-goals

The branch/repo collapse, the D1 lineage merge (seam 3, collapse-time), the IdP
question, attribution changes.
