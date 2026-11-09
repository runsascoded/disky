# Marin GCS usage: ownership & staged deletion, an API for agents

This repo powers <https://gcs.oa.dev>: **who owns what** across the `marin-*` GCS buckets, and a **staged-deletion** workflow to reclaim space. Anyone signed in can **stage** prefixes for deletion (the trash gesture in the UI, or the API below); admins review the shared plan at [`/staged`](https://gcs.oa.dev/staged) and dispatch the executor. **Nothing is deleted at stage time**: staging is a proposal, reversible until an admin runs it. Ownership is the other axis: anyone signed in can **assign** a prefix to anyone (themselves included), or clear an assignment; the ledger records who did what.

This file is the whole guide. Point your agent at it (in the [GitHub repo][API.md], or served unauthenticated at <https://gcs.oa.dev/llms.txt>) and ask it to "find my stuff, stage what's deletable, assign what I know the owner of". Copy-pasteable versions of each step are under [Recipes](#3-recipes).

(The earlier opt-out "mark & sweep" model, keep/sweep marks plus a deadline, was retired 2026-09-28; `dt-cloud mark|status|todo` and `/api/marks*`, `/api/resolve`, `/api/todo` are gone.)

## 1. Get a token

Every write is authenticated as **you** by a personal bearer token.

- **Browser:** sign in at <https://gcs.oa.dev> with your `@openathena.ai` (or whitelisted) email, then open the avatar menu (top-right) → **Agent / CLI Token…** to mint (or rotate) it.
- **API:** `POST /api/token` from a signed-in browser session mints (or rotates) it; the raw token is shown **once**. `GET /api/token` reports status (never the token); `DELETE /api/token` revokes it.

**If you're an agent and have no token, ask your human for one** — e.g. *"Please open https://gcs.oa.dev, click your avatar (top-right) → Agent / CLI Token…, mint a token, and paste it here (or export it as `GCS_USAGE_TOKEN`)."* If they can't sign in, they request access from the sign-in page. Never ask for their password or a browser session cookie; the token is the only credential an agent needs, and they can revoke it from the same menu.

The token carries the `gcs` and `gcs:assign` scopes (least privilege: it can read, stage and assign, not administer). Revoking it is instant. Share links (guest access) can't assign.

Automated readers can instead use an independently revocable `gcs:read` grant: reads only, no staging, assigning or administration. The daily snapshot job uses a dedicated Secret Manager credential, separate from the sheet mirror; operators provision it with `dt-cloud job-token` as described in [GCS operations][operations]. Do not reuse a personal write-capable token for a read-only job.

```bash
export GCS_USAGE_TOKEN=…                  # the token you copied
export GCS_USAGE_URL=https://gcs.oa.dev   # the site
```

Directory prefixes are `gs://marin-<bucket>/<dir>/…/` (trailing slash), in one of the six `marin-*` buckets. An exact object path has no appended slash and carries `kind: 'object'` in supported write operations. The read APIs' `path=` is the same path without scheme or trailing slash (`marin-us-east5/checkpoints`; empty = all buckets).

**You are `me`.** Your owner id is what `GET /api/me` reports as `user`; `user=me` (`/api/estate`), `lens=user:me` (`/api/subtree`, `/api/diff`, `/api/series`) and `owner: '@me'` (`POST /api/actions`) all resolve to it.

## 2. HTTP API

All under `$GCS_USAGE_URL`. Reads are open to any signed-in viewer; writes need `Authorization: Bearer $GCS_USAGE_TOKEN`. A scan's id is `YYYY-MM-DD`, or `YYYY-MM-DDTHHMM` when a day has more than one scan; `GET /data/scans.json` lists them, newest first, and a `date=` of a bare day means that day's latest scan.

### Identity

| Method & path | Purpose |
|---|---|
| `GET /api/me` | `{ email, user, scopes, admin, via }`: `user` is your canonical owner id (null for a guest link without an email). |

### Staged deletion

| Method & path | Purpose |
|---|---|
| `POST /api/plans/stage` | Stage prefixes: `{ prefixes: [...], note?, as_of? }` → `201 { plan_id, batch_id, staged, covered, absorbed, gone?, as_of }`. A prefix under an already-staged ancestor comes back as `covered` (nothing to add); staging an ancestor `absorbs` its staged descendants (the plan keeps the ancestor). A prefix the newest scan holds nothing under (deleted since your `as_of`, or never there) comes back as `gone` and isn't staged; when every one is gone nothing is written (`batch_id: null`, and `plan_id: null` if no plan is open). `as_of` is the scan date (e.g. `2026-10-07`, one of `/data/scans.json`) you staged from — default the latest scan, `400` if no such scan: a run deletes only objects that scan already had, unchanged, so anything written after it is held back (counted as `skipped_after_as_of` in the dry-run's summary). Re-staging a prefix keeps its original `as_of`. The gesture is all-or-nothing, with no size cap: stage a whole logical set (one person's marks) in ONE request, not in chunks — each request is its own batch on `/staged`. Any signed-in stager. |
| `POST /api/plans/:id/batches/merge` | Fold batches into one: `{ into, ids: [...], note? }` → `{ into, merged, items }`. `into` takes the items, the earliest staging time, and `note` if given; the others are deleted. All must be in the open plan and staged by the same person. The batches' stager, or an admin. |
| `GET /api/plans/staged` | The shared open plan, grouped the way `/staged` shows it: `{ plan, items, batches, emptied, runs }`, items with `prefix, note, added_by, added_ts, batch_id, as_of` (`as_of` null: staged before it existed — the run's scan stands in). Completed batches in `emptied` optionally carry `deletion: { bytes, objects, prefixes }`: recorded outcomes from the real runs that completed those prefixes, independent of current scan sizes. `deletion.prefixes < deleted` means partial historical coverage; absent means unknown, not zero. Earlier attempts and later restores are not folded into these completion totals. |
| `DELETE /api/plans/<id>/items` | Unstage: `{ prefixes: [...] }`. Stagers may remove their own items; admins any. |
| `GET /api/plans`, `GET /api/plans/<id>` | Plans and one plan's items / batches / runs. |
| `POST /api/plans`, `PATCH /api/plans/<id>`, `POST /api/plans/<id>/items` | Admin curation (create, close `{ state: 'closed' }`, add items). |
| `POST /api/sweep/dispatch` | Admin: run the executor over a plan (`{ plan_id, mode: 'dry' \| 'real', date, buckets?, shown_min_soft_delete_days? }` → a GCP Batch job; `shown_min_soft_delete_days` is the soft-delete minimum whoever confirmed read, omitted = 7, and a `real` run answers 409 if the current minimum is lower); `GET /api/sweep/jobs` lists runs, `POST /api/sweep/stop` stops one. |

### Deletion runs (what was deleted, and asking for an undo)

Every run, dry or real, is readable by any signed-in viewer; only admins dispatch, stop or undo one.

| Method & path | Purpose |
|---|---|
| `GET /api/plans/staged` | The open plan's items, plus `runs`: every deletion run (dry and real), with `run_id`, `mode`, totals (`deleted_bytes`, `deleted_objects`), its `undo_deadline`, the soft-delete minimum it ran under (`min_soft_delete_days`) and the narrowest retention its buckets had (`soft_delete_days`; 0: no undo). |
| `GET /api/sweep/jobs` | The executor's Batch jobs (runs and undos), live state included. |
| `GET /api/plans/run?id=<run_id>` | One run: `{ run, bands }`, one band per deleted prefix (`prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects`). Run ids hold a `/`: URL-encode them. |
| `GET /api/plans/prefix-history?id=<plan_id>` | Each staged prefix of a plan → the real run(s) that deleted it. |
| `GET /v1/files/list?prefix=sweep/runs/<run_id>/` | A run's directory (plan, per-bucket manifests of the exact objects + generations deleted); fetch a file with `/v1/files/get?path=…`. |

Deleted objects stay recoverable for as long as their bucket retains soft deletes, from each object's deletion, not from job completion; the recorded run deadline is not a new retention window. A real run refuses a bucket retaining less than the deployment's minimum (`GET /api/sweep/settings`: `min_soft_delete_days`, 7 unless an admin has changed it). A run with `soft_delete_days: 0` has **no undo**: a bucket in it had soft delete off, so what it deleted there is permanent, and its `undo_deadline` is null. Ordinary agent tokens cannot restore. Admins can dispatch whole-run recovery through `POST /api/sweep/undo` (`{ run_id, bulk? }`) after the real job finishes and drains, within its recovery window. For prefix-filtered recovery, ask an admin to use the CLI with committed decision logs. Send the run id and prefixes (a reply on the run's thread, or a DM); an agent can assemble them with the recipe below. `/runs/<run_id>` shows progress, deletion records and recovery information; opening it does not start recovery.

### Ownership

| Method & path | Purpose |
|---|---|
| `GET /api/owners?date=<scan>` | Per-user owned bytes + storage-class mix for a scan (what `/users` ranks): `{ scan, head, bytes, objects, users: { <id>: { b, mix } } }`. |
| `GET /api/estate?date=<scan>&user=<id \| me>` | One user's estate: `{ user, date, head, bytes, objects, mix, assignments }`; `assignments` are their live assignments, each `{ prefix, ts, bytes, objects }`. |
| `GET /api/assignments?date=<scan>` | The assigner × assignee matrix (who assigned what to whom, in bytes). |
| `GET /api/actions` | The live ownership ledger: `{ owners: [...] }`, every expanded prefix (`prefix, owner, ts, who, memo, action_id`). `?log=1` is every action, newest first, with its status. |
| `POST /api/actions` | Assign: one action or an array (≤500) of `{ pattern, owner, memo?, scan?, kind?, allow_missing? }`. `owner` is a user id (as `/api/owners` keys them), `'@me'` resolves to you, `null` clears. `pattern` is a directory prefix (`gs://marin-…/dir/`), or one object's exact path with `kind: 'object'`. An assignment whose pattern the newest scan holds nothing under (a typo, or an example path) is refused with `400 { error, missing: [...], scan }` and nothing in the batch is written; pass `allow_missing: true` on an action to assign a path before its data lands. Clears (`owner: null`) are never refused. The newest assignment on a prefix or any ancestor wins. For more than 500, send several arrays (e.g. a search's matches, page by page). |

### Data

| Method & path | Purpose |
|---|---|
| `GET /data/scans.json`, `/data/<scan>/{tree,age,meta}.json`, `/data/rules.json` | The published per-scan artifacts the UI renders (scans.json = the scan ids, newest first; tree = size-floored rollup; meta = totals + per-user/class bytes). |
| `GET /api/subtree?date=&path=&w=&h=` | Pixel-budget subtree of any path: `{ tree, … }`, UI-shaped nodes `{ n, k, b, o, d, a, us, cb, c }` (`n` = the name segment, `b` bytes, `o` objects, `us` = top owners `[[id, bytes], …]`, `c` children; unowned = `b` − Σ `us`), folded to what a w×h canvas can draw (a fold is an `(other)` node with `f`). Scopes: `lens=user:<id \| me>` (one owner's bytes), bare `o` (the unowned pool), `o=*` (the owned pool), `depth=N` (levels below `path`). |
| `GET /api/diff?from=&to=&path=` | What changed under a path between two scans; same scopes. |
| `GET /api/series?path=` | Bytes under a path per scan; same scopes. A scoped series may come in windows of scans: a body with `chunks: n` holds only the newest; `&chunk=i` (0 ≤ i < n, oldest first) reads each of the others. |
| `GET/HEAD /api/path-index?date=` | The **floor-free** path index behind `/api/subtree`, as raw parquet with HTTP Range support: bring your own query engine (see below). One row per rolled-up path × owner slice: `(path, depth, usr, b, o, wts, wb, c2, c3, c4, a)`, sorted `(depth, path)`; `usr` NULL = unowned. |
| `GET /v1/files/<path>` | Raw scan-store proxy (range-supporting) over `listing/` + `snapshots/`: the per-object listing parquets, `dir-cache/`, the index tiers (`index/<gen>/path-index*.parquet`; older scans have them at `listing/<date>/` directly), and published snapshot JSONs, addressed by bucket path. |

Human-facing pages, same data: [`/staged`](https://gcs.oa.dev/staged) is the deletion console, [`/users`](https://gcs.oa.dev/users) the per-user estate ranking, `/user/<id>` one user's estate, `/assignments` the assignment heatmap, and `/runs/<id>` one deletion run (progress, logs, what it deleted, recovery).

```sql
-- DuckDB, straight against prod (httpfs sends HEAD + range GETs, so a
-- depth/path predicate only fetches the row groups it needs):
CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <token>'});
SELECT path, sum(b) AS bytes FROM read_parquet('https://gcs.oa.dev/api/path-index?date=2026-08-26')
WHERE depth = 2 GROUP BY path ORDER BY bytes DESC LIMIT 20;
```

### Activity-summary refresh (admin only)

`POST /api/activity/render?burst=ID&burst=ID` regenerates the selected activity summaries in their configured Slack/Discord sinks; `?since=YYYY-MM-DD` selects groups active since that date. An optional JSON body `{ "reason": "make visited-page links clickable" }` records the purpose (trimmed to 240 characters). The audit event retains unique requested and rendered IDs, the date selection and purpose. Slack/Discord show requested/rendered/skipped counts and links to up to five rendered groups; repeated refresh lines count requests explicitly. Skipped groups are missing or have no reported event kinds. The returned `rendered` IDs name processed groups, not delivery receipts from Slack/Discord. Existing events that recorded only a count cannot recover their original targets.

### Temporary session-monitor API (operator only)

`POST /api/session-monitor` runs the time-boxed GCS aggregate checker; `?dry=1` reads counts without state writes or notifications. It requires the dedicated `gcs:session-monitor:read` grant, not a normal viewer or admin token. That grant has no access to raw session logs, ordinary viewer data or app mutations. Production notifications are capped at three attempts per UTC day, at least an hour apart; preview refuses notification sends. The checker and its grant expire at 2026-10-21T00:00:00Z. It also does nothing when logging is off. This is a recorded-client-event checker, not a full visitor count or uptime guarantee.

## 3. Recipes

`bash` + `curl` + `jq`. Run the setup once; every recipe after it assumes it.

```bash
gu() { curl -sS --fail-with-body -H "Authorization: Bearer $GCS_USAGE_TOKEN" "$@"; }
DATE=$(gu "$GCS_USAGE_URL/data/scans.json" | jq -r '.[0]')   # the latest scan
# A subtree's nodes as rows with full paths (folds dropped), biggest first: jq -f rows.jq
cat > rows.jq <<'EOF'
def rows($p): (.c // [])[] | select(.f == null)
  | (if $p == "" then .n else "\($p)/\(.n)" end) as $q
  | ({ path: $q, gb: (.b / 1e9 * 10 | round / 10), objects: .o, owners: (.us // []) }, rows($q));
[.path as $p | .tree | rows($p)] | sort_by(-.gb) | .[:30][]
EOF
```

**Who am I / my owner id**

```bash
gu "$GCS_USAGE_URL/api/me" | jq '{ email, user, scopes }'
```

**My estate, and my biggest paths**

```bash
# Owned bytes + class mix, and the prefixes assigned to you, biggest first
gu "$GCS_USAGE_URL/api/estate?date=$DATE&user=me" \
  | jq '{ user, bytes, objects, mix, assignments: (.assignments | sort_by(-.bytes)) }'
# Your bytes (assigned or inferred) two levels deep under a path; drill by changing `path`
gu "$GCS_USAGE_URL/api/subtree?date=$DATE&path=&lens=user:me&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**The biggest unowned paths under a prefix**

```bash
# bare `o` = the unowned pool (`o=*` = the owned pool)
gu "$GCS_USAGE_URL/api/subtree?date=$DATE&path=marin-us-east5/checkpoints&o&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**Stage prefixes for deletion, with a note**

```bash
gu -X POST "$GCS_USAGE_URL/api/plans/stage" -H 'Content-Type: application/json' \
  -d '{"prefixes":["gs://marin-us-east5/scratch/old-run/","gs://marin-us-central2/tmp/2025/"],"note":"superseded by run 42"}' \
  | jq '{ plan_id, staged, covered, absorbed, gone }'
```

**Unstage**

```bash
PLAN=$(gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.plan.id')
gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.items[] | [.prefix, .added_by, .note] | @tsv'   # what's staged
gu -X DELETE "$GCS_USAGE_URL/api/plans/$PLAN/items" -H 'Content-Type: application/json' \
  -d '{"prefixes":["gs://marin-us-east5/scratch/old-run/"]}'
```

**Assign: to me, to someone, or clear**

```bash
assign() { gu -X POST "$GCS_USAGE_URL/api/actions" -H 'Content-Type: application/json' -d "$1"; }
assign '{"pattern":"gs://marin-us-east5/checkpoints/my-run/","owner":"@me","memo":"my sweep"}'
gu "$GCS_USAGE_URL/api/owners?date=$DATE" | jq -r '.users | keys[]'   # the user ids
assign '{"pattern":"gs://marin-us-east5/checkpoints/their-run/","owner":"<id>","memo":"their W&B run"}'
assign '{"pattern":"gs://marin-us-east5/checkpoints/shared/","owner":null,"memo":"shared, no single owner"}'
```

The newest assignment on a prefix or any ancestor wins, so assigning (or clearing) an ancestor overrides older assignments below it. Assign the deepest prefix that names what you mean.

**What did run X delete?**

```bash
gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.runs[] | select(.mode == "real") | [.run_id, .deleted_bytes, ((.undo_deadline | numbers | todate) // "-")] | @tsv'
gu -G "$GCS_USAGE_URL/api/plans/run" --data-urlencode "id=<run_id>" \
  | jq -r '.bands[] | [.prefix, .bytes, .objects, .undone_objects] | @tsv'
```

**Is my prefix in a deleted run?**

```bash
P=gs://marin-us-east5/checkpoints/my-run/
for r in $(gu "$GCS_USAGE_URL/api/plans/staged" | jq -r '.runs[] | select(.mode == "real") | .run_id'); do
  gu -G "$GCS_USAGE_URL/api/plans/run" --data-urlencode "id=$r" | jq -r --arg p "$P" \
    '.run as $run | .bands[] | .prefix as $b | select(($b | startswith($p)) or ($p | startswith($b)))
     | [$run.run_id, $b, .bytes, (($run.undo_deadline | numbers | todate) // "-")] | @tsv'
done
```

**Asking for an undo.** Recovery is an admin's call; ordinary agent tokens cannot dispatch it. Message an admin (a reply on the run's Slack/Discord thread, or a DM) with the run id, prefixes you want back (the recipe above lists them), and why, before their recovery cutoff. An admin uses the whole-run recovery API or the prefix-filtered CLI after stop/drain.

## Notes for agents

- **Staging is reversible until dispatch.** Unstage any time (`DELETE /api/plans/<id>/items`); only an admin's `real` run deletes, and a run is undoable only while its buckets retain soft deletes, and not at all with soft delete off (see `/staged`).
- **Browse first:** `/api/subtree` (or the parquet index) tells you what a prefix holds and who owns it; stage the deepest prefixes that name what you mean, not a fat ancestor: an ancestor absorbs everything under it.
- **Explain yourself:** the `note` on a stage batch is what the reviewing admin reads on `/staged`; the `memo` on an assignment is what the ledger shows.
- There is no client CLI for staging; the surviving `dt-cloud` verbs are the pipeline's and the executor's (`healthcheck`, `sweep manifest --plan`, …).

[API.md]: https://github.com/Open-Athena/disky/blob/gcs/API.md
[operations]: specs/gcs-operations.md
