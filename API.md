# Storage usage: an API for agents

This site maps the object-store buckets its scans cover: what is where, how big, how old, and (where the deployment keeps ownership) who owns it. Depending on its configuration it also lets signed-in people **assign owners** to prefixes and **stage** prefixes for deletion, which admins review and run.

This file is the whole guide, served unauthenticated at `<origin>/llms.txt` (`<origin>` = the site you are reading this from). Copy-pasteable versions of each step are under [Recipes](#4-recipes).

## 1. Get a token

Reads and writes authenticate as **you** with a personal bearer token.

- **Browser:** sign in at `<origin>`, open the avatar menu (top-right) → **Agent / CLI Token…**, and mint (or rotate) it. The dialog shows the raw token once, with a ready-to-paste snippet.
- **API:** from a signed-in browser session, `POST /api/token` mints (or rotates: the old token is revoked first) and returns `{ token, created }`, the raw token **once**. `GET /api/token` → `{ active, created }` (never the token); `DELETE /api/token` → `{ revoked }`. A token can't mint a token (403).

**If you're an agent and have no token, ask your human for one**, e.g. *"Please open `<origin>`, click your avatar (top-right) → Agent / CLI Token…, mint a token, and paste it here (or export it as `SITE_TOKEN`)."* If they can't sign in, they ask an admin for access. Never ask for a password or a browser session cookie: the token is the only credential an agent needs, and they can revoke it from the same menu.

The token carries the deployment's viewer scope and, when their session has it, its assign scope (`<base>` and `<base>:assign`, where `<base>` is the deployment's `BASE_SCOPE`): it can read, stage and assign, never administer. Revoking it is instant. A read-only share link or grant (`<base>:read`) reads, but can't stage or assign.

A **public** deployment (`PUBLIC_READ`) serves every read anonymously; you need a token there only to write.

```bash
export SITE_TOKEN=…           # the token you copied
export SITE_URL=<origin>      # the site, e.g. https://example.org
```

## 2. Paths, scans, and you

- **Paths.** The read APIs' `path=` is a path under the store root, without scheme or trailing slash: `<bucket>/<dir>/<subdir>`; empty = every bucket (the root's children are the buckets).
- **Prefixes** (what writes take) are `<scheme><bucket>/<dir>/…/`, with a trailing slash, in the deployment's scheme (`gs://`, `s3://`, …; the token dialog's example uses it, and a malformed prefix's 400 names it). One exact object is its key with no trailing slash, plus `kind: 'object'` (assignments) or listed under `objects` (staging).
- **Scans.** A scan's id is `YYYY-MM-DD`, or `YYYY-MM-DDTHHMM` (UTC) when it carries a time. `GET /data/scans.json` lists the ids, newest first (a deployment whose snapshots sit in a subdirectory serves it as `/data/<subdir>/scans.json`, and the bare one is a 404). `date=`, `from=` and `to=` take an exact id: an id naming no indexed scan is a 404, never the nearest scan. Where a route accepts `d=` instead of `date=` (`/api/subtree`, `/api/owners`, `/api/estate`, `/api/assignments`, `/api/path-index`), `d=<slug>` names the latest scan the slug matches: `YYMMDD` a day, `YYMMDDHH` an hour, `YYMMDDHHMM` a minute.
- **You are `me`.** Your owner id is what `GET /api/me` reports as `user`; `user=me` (`/api/estate`), `lens=user:me` (`/api/subtree`, `/api/diff`, `/api/series`) and `owner: '@me'` (`POST /api/actions`) all resolve to it.

## 3. HTTP API

All under `$SITE_URL`. Reads need a viewer (any signed-in person, a token, or nobody on a public deployment); writes need `Authorization: Bearer $SITE_TOKEN`. Errors are JSON `{ error }` on most routes and plain text on the map routes (`/api/subtree`, `/api/diff`, `/api/series`): use `curl --fail-with-body` to see either.

### What depends on the deployment

Not every deployment has every section. Each is switched by the site's configuration, and a route that isn't switched on answers as below:

| Section | Present when | Otherwise |
|---|---|---|
| Ownership (`/api/actions`, `/api/owners`, `/api/estate`, `/api/assignments`) | the deployment's D1 has the ownership ledger (tables `actions`, `owner_prefixes`); assignments also need the scanned buckets named (`STORE_BUCKETS`) | 404 `no ownership ledger on this deployment` |
| Staging (`POST /api/plans/stage`, unstaging your own items, merging your batches) | `STAGING` is set | 404 `this deployment does not stage`: admins curate plans directly |
| Deletion runs' jobs (`/api/sweep/*` or `/api/plan-sweep/*`) | the executor named by `EXECUTOR`: `sweep` → `/api/sweep/*`, `plan-sweep` (the default) → `/api/plan-sweep/*`; listing and dispatch also need the dispatch credentials (`GCP_SA_KEY`, …) | `jobs` lists `{ jobs: [], configured: false }`; dispatch 503 |
| `/api/path-index` | a scan the interval store doesn't hold (`PATH_STORE` unset, or the scan predates the store) | 410 with the `dt-cloud interval-store export` command that writes the scan's rows |
| `/v1/files` | the store's allow-listed prefixes (`STORE_PREFIXES`; default `listing/`, `snapshots/`, `sweep/`): `GET /api/store` → `{ uri, prefixes }` | 404 outside them |

Owner scopes (`lens=`, `o=`) work wherever the scans carry owner attribution; on a ledger deployment the live ledger's assignments repaint it.

### Identity

| Method & path | Purpose |
|---|---|
| `GET /api/me` | `{ email, user, scopes, admin, via }`: `user` is your canonical owner id (null without an email: a guest link, or anonymous on a public deployment); `via` is `session`, `grant` (a token or link) or `public`. |

### Map data

| Method & path | Purpose |
|---|---|
| `GET /data/scans.json` | The scan ids, newest first (see §2). `/data/<scan>/meta.json` is that scan's summary (`total_bytes`, `total_objects`, …), `/data/<scan>/age.json` its age histogram, `/data/rules.json` the attribution rules. |
| `GET /api/subtree?date=&path=&w=&h=` | A pixel-budget subtree of any path: `{ date, path, threshold, nodes, truncated, tree, … }`. Each node is `{ name, kind, bytes, objects, mtime?, last_read?, owners?, unknown_owner_bytes?, class_bytes?, children?, folded? }`: `name` the path segment (the root's is the path's label), `kind` `dir` or `file`, `bytes` / `objects` at or under it, `mtime` the newest write (epoch seconds), `last_read` the newest read (epoch days; only where access logs are ingested), `owners` the known owners `[[id, bytes], …]` largest first, `unknown_owner_bytes` bytes whose ownership wasn't read (absent: zero; not unowned), `class_bytes` non-Standard storage class id → bytes. Known unowned = `bytes − Σ owners − unknown_owner_bytes`. Children are folded to what a `w`×`h` canvas (default 1280×800) can draw: a fold is an `(other)` node whose `folded` counts the children it stands in for. Scopes: `lens=user:<id \| me>` (one owner's bytes), bare `o` (the unowned pool), `o=*` (the owned pool), `depth=N` (at most N levels below `path`), `q=` (a name filter: the map's search box; a deployment that answers search only from its prebuilt name index refuses a form it can't answer with a 400 and a reason). |
| `GET /api/diff?from=&to=&path=` | What changed under a path between two scans (`from` before `to`, exact ids), with the same scopes and canvas budget: `{ prev, curr, path, bytes_before, bytes_after, objects_before, objects_after, rows, truncated, … }`, each row `{ path, depth, kind, status, bytes_before, bytes_after, objects_before, objects_after, expanded? }` (`status` `added` \| `removed` \| `changed` \| `unchanged`; `path` relative to the request's; `expanded`: the row's change is carried by its children). `summary=1` answers the totals only, `rows` empty. |
| `GET /api/series?path=` | Bytes under a path per scan, with the same scopes: `{ path, points: [{ date, bytes, objects }, …] }`, oldest first. A scoped series may come in windows of scans: a body with `chunks: n` holds only the newest; `&chunk=i` (0 ≤ i < n, oldest first) reads each of the others. |
| `POST /api/prefixes` | `{ date, prefixes: [...] }` (1–1000 prefixes, `<scheme><bucket>/…/`) → `{ date, stats: { <prefix>: { bytes, objects, mtime?, last_read?, owners?, class_bytes? } }, groups }`: many prefixes sized at one scan in one request. A prefix holding nothing at that scan is absent from `stats`. |
| `GET/HEAD /api/path-index?date=` | The floor-free path index behind the map for one scan, as raw parquet with HTTP Range support, for your own query engine (DuckDB, pyarrow). One row per path × owner slice, sorted `(depth, path)`, with `path`, `depth` and `usr` (the slice's owner; NULL = unowned) beside size columns whose names depend on when the scan was indexed (`b`/`o` or `size`/`n_files`): `DESCRIBE` it first. A GET without `Range` is a 416 stating the size; one range is at most 64 MiB. See the table above for scans it doesn't serve. |
| `GET /v1/files/list?prefix=`, `GET /v1/files/get?path=` | The scan store's raw files (listings, snapshots, deletion-run records) under the allow-listed prefixes: `list` pages a prefix (`cursor`, `limit`), `get` reads one file (Range supported). |

Human-facing pages, same data: `/` is the map, `/scans` the scans and their runs, `/staged` the deletion console, `/runs/<run_id>` one deletion run, and, where there's ownership, `/users`, `/user/<id>` and `/assignments`.

### Ownership

Where the deployment keeps an ownership ledger (see the table above). Anyone signed in with the assign scope (every full viewer, and their token) can assign any prefix to anyone, themselves included, or clear an assignment; the ledger records who did what.

| Method & path | Purpose |
|---|---|
| `GET /api/owners?date=<scan>` | Per-owner bytes for a scan (what `/users` ranks): `{ scan, head, bytes, objects, users: { <id>: { bytes, class_bytes } }, computed }`. |
| `GET /api/estate?date=<scan>&user=<id \| me>` | One owner's estate: `{ user, date, head, bytes, objects, mix, assignments }`; `assignments` are their live assignments, each `{ prefix, ts, bytes, objects, repainted_by? }`. |
| `GET /api/assignments?date=<scan>` | The assigner × assignee matrix (who assigned what to whom, in bytes). |
| `GET /api/actions` | The live ledger: `{ owners: [...] }`, every live assignment (`prefix, kind, owner, ts, who, memo, action_id`). `?log=1` (`&limit=&offset=`) is every action, newest first, with its status. |
| `POST /api/actions` | Assign: one action or an array (1–500) of `{ pattern, owner, memo?, scan?, kind?, allow_missing? }`. `owner` is an owner id (as `/api/owners` keys them), `'@me'` resolves to you, `null` clears. `pattern` is a directory prefix (`<scheme><bucket>/<dir>/`), or one object's exact key with `kind: 'object'`. An assignment whose pattern the newest scan holds nothing under (a typo, an example path) is refused with `400 { error, missing: [...], scan }` and nothing in the request is written; `allow_missing: true` on an action assigns a path before its data lands. Clears are never refused. → `{ ok, count, ts }`. |

The newest assignment on a prefix or any ancestor wins, so assigning (or clearing) an ancestor overrides older assignments below it. Assign the deepest prefix that names what you mean.

### Staged deletion

Plans, items and runs are readable wherever the deployment has a D1. **Staging** (anyone with the full viewer scope proposing deletions) is on where `STAGING` is set. **Nothing is deleted at stage time**: staging is a proposal, reversible until an admin runs it.

| Method & path | Purpose |
|---|---|
| `POST /api/plans/stage` | Stage: `{ prefixes: [...], objects?: [...], note?, as_of? }` → `201 { plan_id, batch_id, staged, staged_objects?, covered, absorbed, gone?, as_of }`. Items join the shared open plan (one is created if none is open), as one batch carrying `note`. A prefix under an already-staged ancestor comes back `covered` (nothing added); staging an ancestor `absorbs` its staged descendants. An item the newest scan holds nothing under comes back `gone` and isn't staged; when nothing is new, nothing is written (`batch_id: null`; `plan_id: null` if no plan is open). `as_of` is the scan you staged from (default the newest; `400` if no such scan), recorded per item; re-staging an item keeps its first batch and `as_of`. One request is one batch on `/staged`, all or nothing: stage a whole logical set in one request. Always send full `<scheme><bucket>/…/` prefixes: a prefix naming no scanned bucket is read as under the first. |
| `GET /api/plans/staged` | The shared open plan: `{ plan, items, batches, emptied, runs }` (`{ plan: null, … }` if none is open). Items are `{ prefix, kind, note, added_by, added_ts, batch_id, as_of }` (`as_of` null: staged before it was recorded). `emptied` are batches with no items left, and what became of them (`staged`, `covered`, `absorbed`, `unstaged`, `deleted`, and `deletion: { bytes, objects, prefixes }` where real runs recorded completing them). `runs` are the deployment's deletion runs (all plans, newest first). |
| `DELETE /api/plans/<id>/items` | Unstage: `{ prefixes: [...] }` → `{ id, removed }`. With staging on, you may remove items you staged (403 on anyone else's); admins any. |
| `POST /api/plans/<id>/batches/merge` | Fold batches into one: `{ into, ids: [...], note? }` → `{ into, merged, items }`. All in the open plan and staged by one person; that person, or an admin. |
| `GET /api/plans`, `GET /api/plans/<id>` | Every plan (with item and run counts), and one plan's items, batches and runs. |

### Deletion runs

Any viewer can read every run, dry or real; only admins dispatch, stop or undo one.

| Method & path | Purpose |
|---|---|
| `GET /api/plans/run?id=<run_id>` | One run: `{ run, bands }`. `run` holds `run_id, plan_id, scan, mode` (`dry` \| `real`), `started_ts, finished_ts, deleted_bytes, deleted_objects, undo_deadline` (epoch s; null: no undo), `undo_state`, `min_soft_delete_days`, `soft_delete_days`, …; one band per prefix, largest first: `{ prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects }`. Run ids may hold a `/`: URL-encode them. |
| `GET /api/plans/prefix-history?id=<plan_id>` | `{ bands }`: each of a plan's items → the real runs (since it was staged) that deleted it, with each run's `started_ts`, `finished_ts`, `undo_deadline`, `undo_state`. |
| `GET /api/plans/progress?id=<run_id>` | `{ samples }`: a run's cumulative delete progress over time. |
| `GET /api/sweep/jobs` or `GET /api/plan-sweep/jobs` | The executor's recent Batch jobs (runs, and undos where the executor has them), with live state. |
| `GET /api/sweep/settings` | `{ settings, min_soft_delete_days }`: the soft-delete retention a real run requires (default 7 on the `sweep` executor, 0 on `plan-sweep`). |
| `GET /api/sweep/soft-delete` | `{ soft_delete: { asOf, off: { <bucket>: boolean } } \| null }`: whether each bucket's soft delete was off at the job's newest reading (null: never read here). |
| `GET /v1/files/list?prefix=sweep/runs/<run_id>/` | A run's directory (its plan, manifests of the exact objects deleted, logs), where `sweep/` is allow-listed. |

A run's `undo_deadline` is when its recovery window closes; a run with no deadline has no undo. Ordinary tokens can't restore anything: **ask an admin**, with the run id and the prefixes you want back (the recipe below finds them), before the deadline. `/runs/<run_id>` shows a run's progress, what it deleted and its recovery state.

### Admin only

Admins (the `admin` scope; never on a personal token) also: create, close and curate plans (`POST /api/plans`, `PATCH /api/plans/<id>`, `POST`/`DELETE /api/plans/<id>/items`), dispatch, stop and undo runs (`POST /api/sweep/{dispatch,stop,undo}` or `/api/plan-sweep/{dispatch,stop,undo,purge}`, per `EXECUTOR`), and set `PUT /api/sweep/settings`. An agent holding a personal token gets 403 on these.

## 4. Recipes

`bash` + `curl` + `jq`. Run the setup once; every recipe after it assumes it. On a public deployment, reads work without the header.

```bash
api() { curl -sS --fail-with-body -H "Authorization: Bearer $SITE_TOKEN" "$@"; }
DATE=$(api "$SITE_URL/data/scans.json" | jq -r '.[0]')   # the newest scan
# A subtree's nodes as rows with full paths (folds dropped), biggest first: jq -f rows.jq
cat > rows.jq <<'EOF'
def rows($p): (.children // [])[] | select(.folded == null)
  | (if $p == "" then .name else "\($p)/\(.name)" end) as $q
  | ({ path: $q, gb: (.bytes / 1e9 * 10 | round / 10), objects: .objects, owners: (.owners // []) }, rows($q));
[.path as $p | .tree | rows($p)] | sort_by(-.gb) | .[:30][]
EOF
```

**Who am I / my owner id**

```bash
api "$SITE_URL/api/me" | jq '{ email, user, scopes, via }'
```

**The biggest paths under a path**

```bash
# path= without scheme or trailing slash; empty = every bucket. Drill by changing it.
api "$SITE_URL/api/subtree?date=$DATE&path=<bucket>/<dir>&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**What grew or shrank under a path between two scans**

```bash
PREV=$(api "$SITE_URL/data/scans.json" | jq -r '.[1]')   # the scan before the newest
api "$SITE_URL/api/diff?from=$PREV&to=$DATE&path=<bucket>/<dir>&w=4096&h=4096" \
  | jq -r '.rows | map(select(.expanded == null)) | sort_by(-((.bytes_after - .bytes_before) | fabs)) | .[:30][]
           | [.status, .path, (.bytes_after - .bytes_before)] | @tsv'
```

**Size over time**

```bash
api "$SITE_URL/api/series?path=<bucket>/<dir>" | jq -r '.points[] | [.date, .bytes, .objects] | @tsv'
```

**My estate, and my biggest paths** (ownership deployments)

```bash
# Owned bytes + class mix, and the prefixes assigned to you, biggest first
api "$SITE_URL/api/estate?date=$DATE&user=me" \
  | jq '{ user, bytes, objects, mix, assignments: (.assignments | sort_by(-.bytes)) }'
# Your bytes two levels deep under a path
api "$SITE_URL/api/subtree?date=$DATE&path=&lens=user:me&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**The biggest unowned paths under a path**

```bash
# bare `o` = the unowned pool (`o=*` = the owned pool)
api "$SITE_URL/api/subtree?date=$DATE&path=<bucket>/<dir>&o&depth=2&w=4096&h=4096" | jq -f rows.jq
```

**Assign: to me, to someone, or clear** (ownership deployments)

```bash
assign() { api -X POST "$SITE_URL/api/actions" -H 'Content-Type: application/json' -d "$1"; }
assign '{"pattern":"<scheme><bucket>/<dir>/my-run/","owner":"@me","memo":"my experiment"}'
api "$SITE_URL/api/owners?date=$DATE" | jq -r '.users | keys[]'   # the owner ids
assign '{"pattern":"<scheme><bucket>/<dir>/their-run/","owner":"<id>","memo":"their run"}'
assign '{"pattern":"<scheme><bucket>/<dir>/shared/","owner":null,"memo":"shared, no single owner"}'
```

**Stage prefixes for deletion, with a note** (staging deployments)

```bash
api -X POST "$SITE_URL/api/plans/stage" -H 'Content-Type: application/json' \
  -d '{"prefixes":["<scheme><bucket>/scratch/old-run/","<scheme><bucket>/tmp/2025/"],"note":"superseded by run 42"}' \
  | jq '{ plan_id, staged, covered, absorbed, gone }'
```

**Unstage**

```bash
PLAN=$(api "$SITE_URL/api/plans/staged" | jq -r '.plan.id')
api "$SITE_URL/api/plans/staged" | jq -r '.items[] | [.prefix, .added_by, .note] | @tsv'   # what's staged
api -X DELETE "$SITE_URL/api/plans/$PLAN/items" -H 'Content-Type: application/json' \
  -d '{"prefixes":["<scheme><bucket>/scratch/old-run/"]}'
```

**What did run X delete?**

```bash
api "$SITE_URL/api/plans/staged" | jq -r '.runs[] | select(.mode == "real") | [.run_id, .deleted_bytes, ((.undo_deadline | numbers | todate) // "-")] | @tsv'
api -G "$SITE_URL/api/plans/run" --data-urlencode "id=<run_id>" \
  | jq -r '.bands[] | [.prefix, .bytes, .objects, .undone_objects] | @tsv'
```

**Is my prefix in a deleted run?**

```bash
P=<scheme><bucket>/<dir>/my-run/
for r in $(api "$SITE_URL/api/plans/staged" | jq -r '.runs[] | select(.mode == "real") | .run_id'); do
  api -G "$SITE_URL/api/plans/run" --data-urlencode "id=$r" | jq -r --arg p "$P" \
    '.run as $run | .bands[] | .prefix as $b | select(($b | startswith($p)) or ($p | startswith($b)))
     | [$run.run_id, $b, .bytes, (($run.undo_deadline | numbers | todate) // "-")] | @tsv'
done
```

**Query the path index yourself** (DuckDB; scans `/api/path-index` serves)

```sql
-- httpfs sends HEAD + range GETs, so a depth/path predicate fetches only the row groups it needs
CREATE SECRET (TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer <token>'});
DESCRIBE SELECT * FROM read_parquet('<origin>/api/path-index?date=<scan>');
SELECT * FROM read_parquet('<origin>/api/path-index?date=<scan>') WHERE depth = 2 LIMIT 20;
```

## Notes for agents

- **Browse first:** `/api/subtree` (or `/api/prefixes` for a list you already have) tells you what a prefix holds and who owns it. Stage and assign the deepest prefixes that name what you mean, not a fat ancestor: an ancestor absorbs (staging) or overrides (assigning) everything under it.
- **Staging is reversible until dispatch.** Unstage any time; only an admin's `real` run deletes, and whether it can be undone depends on the buckets' retention (`/api/sweep/soft-delete`, each run's `undo_deadline`).
- **Explain yourself:** a stage batch's `note` is what the reviewing admin reads on `/staged`; an assignment's `memo` is what the ledger shows.
- **A 404 on a whole section** usually means this deployment doesn't have it (see [What depends on the deployment](#what-depends-on-the-deployment)), not that your request was wrong.
