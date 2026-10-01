# Actions ledger: attribution + keep/sweep as one auditable WAL

Unify the two kinds of judgment users make about storage — *whose is it* (owner axis) and *should it survive the sweep* (keep axis) — into a single append-only ledger of *actions*. Today's `marks`/`claims`/`mark_log` tables are a special case of this (keep axis only, prefix patterns only, deepest-wins); this spec generalizes and supersedes them.

## Semantics

**An action** is one user gesture: "this pattern gets this owner and/or this keep-state, and here's why". Actions are immutable and append-only — the ledger *is* the audit log. Current state is the fold of all actions in time order.

**Most-recent-wins, per axis.** For a given path and axis, the effective value comes from the most recent action covering that path that *touched* that axis. Recency beats specificity: a later broad rule paints over earlier deeper marks (that's allowed and intended — "repaint" is the mental model). The two axes resolve independently: a deep owner assignment is not disturbed by a shallower, newer keep mark, and vice versa.

> **Change from shipped behavior**: the live /mark UI resolves deepest-mark-wins. Migrating to recency is fine for existing data (every live mark is prefix-shaped and they rarely nest conflictingly), but the UI must add a guard: before writing a broad mark that covers existing more-specific marks, surface "overrides N more-specific marks (M Ti currently keep)" and require confirm. Silently clobbering someone's deep keep is the failure mode recency introduces.

**Clearing.** An action can reset an axis to unknown (owner → unattributed, keep → unmarked). A clear is just another touch of the axis — most recent wins as usual.

**Claims fold into the owner axis.** "Claim" = assigning owner to yourself. The separate `claims` table retires; the Lost & found claim button becomes an owner action with `owner = <my user>`.

## Patterns and expansion

`pattern` is either a **prefix** (`gs://marin-<bucket>/path/`) or a **regex** (`/…/`, matched against segment names with the same outermost-match semantics as the treemap's `?f=` filter: a matching segment claims its whole subtree).

**Expansion at write time, prefix granularity.** When an action is created, its pattern is expanded against the scan the actor was viewing into concrete prefix rows. This freezes the action's coverage: paths that would match the regex but appear in *later* scans are NOT covered (the "do you claim everything that may ever match?" contract was rejected as unfair). The expansion is what resolution queries.

**Caps (CYA).** Expansion is capped (default **5,000 prefixes**); patterns that exceed it are rejected with the match count and a "narrow it" message. Pattern length ≤ 512 chars. This structurally defuses `/.*/` and `/\.parquet$/`-style patterns: at prefix granularity over the ~48K-node depth-capped tree, even pathological patterns are bounded, and the cap rejects the rest.

**Object-level fidelity is batch-side.** The interactive expanded index is exact for prefix-shaped patterns (the overwhelming majority) and prefix-approximate for regexes. The authoritative **sweep manifest** is re-derived batch-side from *raw* actions against the final scan's full listing (DuckDB over 594M rows is a normal pass there), so an object-level regex intent is honored exactly where it matters.

## Schema (D1)

Three tables: one raw (the audit unit), two expanded (per-axis resolution indexes). Split expanded per axis so the hot path — "resolve owner" or "resolve keep" for a path — is one indexed query with no NULL-skipping, and both can fire in parallel. Raw stays unified so one gesture (owner + keep + memo together) is one audit row.

```sql
CREATE TABLE actions (
  id         INTEGER PRIMARY KEY,
  actor      TEXT NOT NULL,             -- email of the acting identity (writes require one)
  ts         INTEGER NOT NULL,          -- unix seconds, server-assigned
  scan       TEXT NOT NULL,             -- scan date the actor was viewing (= latest at write time)
  pattern    TEXT NOT NULL,             -- prefix or /regex/
  set_owner  INTEGER NOT NULL DEFAULT 0,
  owner      TEXT,                      -- user id; NULL with set_owner=1 = clear to unattributed
  set_keep   INTEGER NOT NULL DEFAULT 0,
  keep       TEXT,                      -- 'keep' | 'sweep' | 'keep_last_ckpt'; NULL with set_keep=1 = unmark
  memo       TEXT,
  CHECK (set_owner OR set_keep)
);
CREATE INDEX idx_actions_actor ON actions (actor, ts);

CREATE TABLE owner_prefixes (
  action_id  INTEGER NOT NULL REFERENCES actions (id),
  prefix     TEXT NOT NULL,
  owner      TEXT,                      -- NULL = cleared
  ts         INTEGER NOT NULL,          -- denormalized from actions.ts (immutable)
  tombstoned TEXT,                      -- scan date the prefix stopped existing; NULL = live
  PRIMARY KEY (prefix, action_id)
);
CREATE INDEX idx_owner_prefix_ts ON owner_prefixes (prefix, ts DESC);

CREATE TABLE keep_prefixes (            -- same shape, keep axis
  action_id  INTEGER NOT NULL REFERENCES actions (id),
  prefix     TEXT NOT NULL,
  keep       TEXT,
  ts         INTEGER NOT NULL,
  tombstoned TEXT,
  PRIMARY KEY (prefix, action_id)
);
CREATE INDEX idx_keep_prefix_ts ON keep_prefixes (prefix, ts DESC);
```

Notes:
- `keep = 'sweep'` replaces today's `'delete'` (absence still defaults to swept at the deadline; an explicit `sweep` is a reviewed judgment, distinct from unreviewed).
- Expanded rows exist only for axes the action touched (row-existence = touched; NULL value = clear). `ts` is denormalized — safe because actions are immutable.
- No scan column on expanded rows: `actions.scan` gives "since", `tombstoned` gives "until".

## Resolution

A path has at most ~depth-cap ancestor prefixes, so resolution enumerates them:

```sql
SELECT owner, action_id FROM owner_prefixes
WHERE prefix IN (?, ?, …)               -- the path's own ancestor prefixes, root→self
  AND tombstoned IS NULL
ORDER BY ts DESC, action_id DESC LIMIT 1
```

(and the same against `keep_prefixes` — the two run in parallel). Client-side, the mark index does the identical fold in memory: load all live expanded rows (small — thousands), resolve per node as `max(ts)` over ancestors per axis. `action_id` tiebreaks equal timestamps.

## Scan binding & lift-over

- **Writes only against the latest scan** (server-enforced): `actions.scan` = latest at write time; older scans are read-only views.
- **Lift-over is free**: prefixes are stable names. A newer scan changes stats, not coverage. Prefixes that disappear are presumed deleted — a tombstone pass at scan ingest sets `tombstoned = <scan date>` on expanded rows whose prefix no longer exists (kept for history, excluded from resolution). Moves need a fresh action; rare and correct.

## Migration & seeding

1. `marks` → one action per row (`set_keep = 1`, `delete` → `sweep`), expanded 1:1 into `keep_prefixes`; `who`/`ts`/`note` carry over.
2. `claims` → owner actions (`set_owner = 1`, `owner =` claimant's user via `user_emails`).
3. `mark_log` → retained read-only for history (it predates the ledger); new writes only append to `actions`.
4. **Seed attribution**: the batch pipeline's manual attribution rules become day-zero owner actions attributed to `ryan-williams` (ts = adoption date), making the ledger the single source of attribution judgment going forward; the batch pipeline reads ledger actions instead of (then alongside, then instead of) the YAML rules.

## Guards

- Writes require an **email-bearing identity** — anonymous/guest grant sessions are read-only; the UI shows a sign-in upsell where write controls would be.
- Expansion cap (5,000) + pattern length cap (512) as above; server-side write rate limit (e.g. 60 actions/min/actor).
- Broad-override confirm in the UI (see Semantics).

## Live stats & rollups

- **Live**: client-side fold over the loaded tree + expanded rows — per-user kept/swept/unmarked totals, review-progress lines (generalizes `reviewedBytes`).
- **Time series**: a nightly rollup writes `sweep_stats (scan, user, kept_bytes, klc_bytes, swept_bytes, unmarked_bytes)` per scan ingest; the fractions-over-time chart reads this. No query-time tree walks.

## Views (UI)

- Treemap **fate lens**: color by kept / keep-last-ckpt / swept / unmarked, with agg stats in the legend.
- **Per-path history** panel: all actions whose expansion covers the current node (pag-tbl), most recent first, with actor AVI + memo.
- **Per-actor listing**: all actions by a user (`/mark/actions?actor=…`).
- **File listing pag-tbl** under the treemap (`@rdub/file-tree`, à la DT's demo) — longstanding TODO, lands with this.
- AVIs: `user_emails.github` column → `avatars.githubusercontent.com/<handle>`; backfill handles from the marin git-log pass that built the roster.

## API (agents welcome)

- `GET  /api/actions?path=&actor=&axis=&limit=` — filtered listing.
- `POST /api/actions` — one action or a batch array; returns per-item results (expansion counts, cap rejections).
- `GET  /api/resolve?path=` — effective owner + keep for a path, with provenance (action id, actor, ts, memo).
- Auth for non-browser clients: a personal token (a grant carrying the caller's email) passed as `Authorization: Bearer`. Writes require the email; guest links 403.
- **`gcs-usage mark` (shipped)** — `marin/src/gcs_usage/mark.py` + CLI, 21 tests. Reads prefixes from args / `--file` / stdin, builds `/api/actions` batches (≤500), `--owner @me --keep keep`, `--dry-run`. Token via `--token` or `$GCS_USAGE_TOKEN`, base URL `$GCS_USAGE_URL` (default gcs.oa.dev). Live contract verified against prod (unauth POST → `401 {"error":"unauthenticated"}`); authenticated write pends the token-reveal below. This is the primitive an agent shells out to; **no MCP** — a CLI with `--help` is already agent-legible, and the MCP would only re-wrap it.
- **Self-service token (header "token" button) — built, pending deploy.** The gate stores only a token *hash* (`mint()` returns the raw exactly once), so there is no "reveal"; the control **mints/rotates**. Endpoint `site/functions/api/token.ts`: `GET` → `{active, created}` (never the token), `POST` → revoke the prior grant then mint a fresh `gcs`-scoped grant carrying the caller's email → `{token, created}` once, `DELETE` → revoke. A `gcs`-scoped, email-carrying, non-expiring grant used as `Authorization: Bearer` authenticates directly every request (gate.js:61-73 — no redeem/consumption), so it behaves as a PAT and revokes instantly. Least privilege: `gcs` only, so a leaked staff token still can't reach admin/cw. Guard: `identify().via` must be edge/session, not `grant` — a grant minting a grant is privilege escalation (403). One grant id per user tracked in `agent_tokens(email, grant_id, created)` (migration `0011`). UI: `TokenModal.tsx` off the header chip (shown when `useCanMark()`), status → generate/rotate/revoke, copy-once value + a `GCS_USAGE_TOKEN` recipe. Type-checks; modal shell verified in-browser (error phase, since plain vite doesn't serve the endpoint). **Remaining: apply migration `0011` to prod D1 + deploy, then live end-to-end (sign in → mint → `gcs-usage mark` → mark lands).** Policy settled: non-expiring + revocable + rotatable; `gcs`-only scope.

## Rollout order

1. **Pre-reveal polish** (shipping now, against the current marks tables): last-ckpt gating + tooltip, lens-scoped treemap per /mark tab, any-user "owned by" view, guest-write guard.
2. **Ledger cutover**: migrations + `/api/actions` + client resolution switch (recency), broad-override confirm, seeded attribution.
3. **On top**: fate lens, history panel, batch endpoint + regex actions, rollup time series, AVIs, batch-side manifest derivation. **`gcs-usage mark` CLI shipped**; regex actions and the self-service token are the next two (see API section).
