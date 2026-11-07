# Share-link hardening: read-only scope, rotate, token self-ID

Three related gaps surfaced while wiring the `/admin` share-link console for a
guest link (Rob). This spec covers all three; **(1) is the priority** and is
entirely app-side (this repo), **(2)/(3)** touch the `@open-athena/auth` package
(coordinate with the cw-s3 / auth session that owns `auth/specs/`).

## 1. A real read-only scope (`gcs:read`)

### Problem
"Read-only" is not a scope today — it is an ad-hoc `if (!id.email) → 403` guard
in `functions/api/actions.ts`. Two consequences:

- A share link minted **with an email** (magic-link binding) gets `id.email` set
  (`authIdentity`: `email: auth.grant.email ?? null`), so it **passes** the
  write-guard and can `POST /api/actions` (mark keep/sweep, reassign owners) on
  the prod ledger. Binding a guest's email silently un-read-only's them.
- `POST /api/plans/stage` (the trash gesture) is gated only by `requireViewer`
  (the base `gcs` scope) with **no email check at all**, so *any* `gcs` viewer —
  even an emailless guest — can stage deletion proposals into the prod plan.

### Design
Introduce `GCS_READ_SCOPE = 'gcs:read'` — a read-only viewer scope.

- `gcs` = full viewer (read **and** write: mark + stage).
- `gcs:read` = read-only viewer (every GET/read endpoint; no writes).

Gate changes in `functions/_lib/auth.ts`:

- `requireViewer` accepts **either** `gcs` or `gcs:read` (all read endpoints
  keep calling it — one change covers them). Implement as `requireAnyScope(ctx,
  [baseScope, baseReadScope])`.
- New `requireWriter(ctx)` requires the full `gcs` scope. The write endpoints
  switch to it, and the ad-hoc `!id.email` guard is **removed** (email stays,
  for owner attribution only).

Endpoint audit (from `grep requireViewer|requireAdmin`):

- **→ `requireWriter` (needs `gcs`)**: `api/actions.ts` POST, `api/plans/[[path]].ts`
  `/stage` POST. (`api/marks.ts` POST — verify; it also has a `!id.email` guard.)
- **stays `requireAdmin`**: plans create/close/items, `sweep/dispatch`, `sweep/stop`.
- **stays `requireViewer` (now accepts `gcs:read`)**: `resolve`, `todo`, `subtree`,
  `path-index`, `series`, `estate`, `sweep-owners`, `marks` GET, `marks/totals`,
  `diff`, `bench`, `claims`, `data/[[path]]`, `plans` GET, `sweep/jobs` GET.

Mint + UI:

- `/admin` mint form gets a **Read-only** checkbox, default **on**. On → mint
  with `scopes: ['gcs:read']`; off → `['gcs']`. (`POST /api/auth/grants` already
  takes arbitrary `scopes`, no package change.)
- Self-tokens (`/api/token`) keep `gcs` — they exist to drive `dt-cloud mark`.
- Frontend: hide write affordances (the children-table trash button, mark
  controls) when `whoami.scopes` lacks `gcs` (has only `gcs:read`), so a
  read-only holder sees a clean read-only dashboard rather than buttons that 403.

Branch parity: cw-s3 mirrors this as `cw:read` (see `branch-parity-discipline.md`).

## 2. Rotate (in addition to revoke)

Rotate = issue a **new token** for the **same grant** (same subject / scopes /
expiry), invalidating the old one — for when a link leaks but the grant is still
wanted. Agent tokens already rotate (`POST /api/token` is "mint/rotate"); share
links have only revoke / disable / enable / PATCH.

- **Upstream** (`@open-athena/auth`): add `gate.rotate(id)` → new `token_hash`
  on the existing grant row (or mint-new-copying-metadata + revoke-old), return
  the raw token once. Add `POST <base>/grants/:id/rotate`.
- **gcs**: a "rotate" button beside "revoke"; on success show the new
  `?key=…` once (reuse the mint reveal panel).

## 3. Token self-ID / last-N chars

"Can a holder ID their token to an admin later?" — **already yes.** `gate.whoami`
returns the grant **`id`** (plus `name`, `subject`, `scopes`, `expiresAt`) to the
holder; the id is explicitly the non-secret identifier "a person can quote back
to an admin." A CLI-token user runs `curl /api/auth/whoami -H 'Authorization:
Bearer <token>'` and reads their grant id/name.

So last-N-chars is a **convenience, not a requirement** — the grant id already
correlates holder ↔ admin table without exposing any token material. If still
wanted (eyeball the table without asking the holder):

- **Upstream**: store a `token_hint` (last 4–6 chars) at mint; return it in the
  grants list. Small, permanent, low-sensitivity.
- **gcs**: show it dim in the holder/created cell.

Prefer surfacing the existing grant **`id`** in the admin table first (zero
security cost) and only add `token_hint` if that proves insufficient.

## Sequencing

1. Read-only scope (this repo) — unblocks minting Rob as genuinely read-only.
2. Surface grant `id` in the admin table (this repo, trivial) — the self-ID path.
3. Rotate (upstream + gcs UI).
4. `token_hint` (upstream + gcs UI) — only if grant-id proves insufficient.

---

## Decided + implemented — read-only scope (2026-09-19)

**GCP IAM reality (informs, doesn't replace, the app gate):** the marin buckets
live in Stanford's `hai-gcp-models`; ~51 individuals hold `roles/editor`/`owner`
there, so they can `gsutil rm` any object directly, outside this app. So the app
gate is a hard boundary only for principals with **no** bucket IAM — chiefly
**guest share links** (the whole reason for a read-only tier). For allowlisted
staff/users the app tiers are a workflow convention, kept deliberately (below).

**Confirmed capability matrix (GCS):**

| Principal | scopes | read | stage (propose trash) | mark (keep/sweep/owner) | dispatch |
|---|---|---|---|---|---|
| Guest share link (read-only) | `gcs:read` | ✅ | — | — | — |
| Guest share link (full) | `gcs` | ✅ | ✅ | — | — |
| Allowlisted non-admin user | `gcs` | ✅ | ✅ | — | — |
| Admin (staff) | `gcs admin …` | ✅ | ✅ | ✅ | ✅ |

Marking is **admin-only**; non-admins participate in deletion only by staging.

**Shipped:**
- `_lib/auth.ts`: `GCS_READ_SCOPE`/`baseReadScope`, `requireAnyScope`; `requireViewer`
  now accepts `gcs`|`gcs:read`; new `requireStager` (needs `gcs`).
- `api/actions.ts` POST + `api/marks.ts` PUT → `requireAdmin` (drop the `!id.email`
  guard). `api/plans/[[path]].ts` `/stage` → `requireStager`. All reads unchanged
  (now admit `gcs:read`).
- Front end: `useCanMark` → admin scope; new `useCanStage` → base scope; the
  children-table trash/select gates on `useCanStage` (guests see a clean read-only
  table). Mint form gains a **Read-only** checkbox (default on → `gcs:read`).

**Still open:** rotate (§2), `token_hint` (§3, likely unneeded — grant `id` covers
self-ID), surface grant `id` in the admin table, and the cw-s3 `cw:read` mirror.

---

## Auth-package adoption — completed (2026-09-19)

Bumped `@open-athena/auth` `a846f7c` → dist `e254545` (subject-in-mint, disable,
rotate) and dropped the mint shim (`functions/api/auth/[[path]].ts` is a clean
delegate again — the package route now persists the subject natively).

The pinned build predated subject-in-mint, so its `POST /grants` silently dropped
`first`/`last`/`avatar` — every minted link lost its identity (D1: `has_subject`
empty). The bump also brings §2's **rotate** (`gate.rotate(id, { endSessions })`,
`POST …/grants/:id/rotate`, admin-gated) — wired as a "rotate" button beside
"revoke" in `/admin` (re-key-only; `endSessions` available in the API).

Schema sync (the site's migration numbering had diverged from the package's, so
its `grants` table lacked the newer columns). New site migrations bring the auth
schema fully level with the package — no divergence carried forward:
- `0027_auth_grants_disable_expiry` (pkg 0007): `disabled_at`, `expiry_ends_sessions`.
- `0028_auth_profiles` (pkg 0008): `profiles` table (opt-in; not yet used).
- `0029_auth_pending_auth` (pkg 0009+0010): `pending_auth` table (opt-in; OIDC/email-code).
- `0030_auth_grant_rotate` (pkg 0011): `sessions_invalid_before` (rotate's `endSessions`).

Apply order: the migrations are additive and safe under the old code, so apply
them first, then deploy the bump (the new grant store selects the new columns).

**Remaining:** surface grant `id` in the admin table; cw-s3 `cw:read` mirror +
the same package adoption.
