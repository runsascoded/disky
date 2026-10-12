# Path scopes: hide a dir from a link (and, later, root a link at one)

A share link (an `@open-athena/auth` grant) carries free-form scopes; disky decides what they mean. This adds path restrictions as scopes, enforced entirely in disky (no auth changes):

- **`<base>:hide:<prefix>`** — the holder sees `<prefix>` as one cell with its aggregate size, and learns nothing strictly below it: no names, no per-path sizes, no search hits, no diff rows, no cards. Several may be given. E.g. `laptop:hide:Users/ryan/Downloads/2025 tax returns`.
- **`<base>:root:<prefix>`** (phase 2) — the holder's view is rooted at `<prefix>`: nothing outside it (ancestors' totals included).

`<base>` is the store's viewer scope (`laptop`, `nas`, …), so a restriction applies to one store. Prefixes are store-relative paths as the treemap names them (no leading slash, no trailing slash). Admins and email sessions are never restricted; only grants carry these scopes.

## Model

`restrictionFor(identity, env) → { hide: string[] } | null` (`_lib/restrict.ts`), null for an unrestricted viewer — every code path below is a no-op then, so unrestricted responses are byte-identical to today's.

`hidden(path, r)`: `path` is strictly below some hide prefix (`p === h` is visible: the hidden dir's own row stays, with its total).

## Enforcement: filter responses on the way out, block what can't be filtered

The readers and caches stay restriction-free (the edge cache, decoded-group LRU, search cache, owner totals all hold full answers keyed without identity — `specs/path-scopes.md` grew out of a route audit that found no single reader choke point). Instead each route that returns paths applies the restriction to its response after any cache read, so a cache hit can never hand a restricted viewer an unrestricted body, and a restricted request never writes a filtered body to a shared key.

| Route | Restricted viewer |
|---|---|
| `/api/subtree` | 404 if `path` is hidden. Tree: a node at a hide prefix keeps its sizes, loses its children (`c`) and child counts; any node below one is dropped. Search (`q=`): drop matched rows under a hide prefix and recount. |
| `/api/diff` | 404 if `path` hidden; drop rows whose full path is hidden. |
| `/api/series` | 404 if `path` or any of `paths` is hidden. |
| `/api/age-pyramid` | 404 if `path` hidden. |
| `/api/prefixes` | hidden prefixes answer as absent. |
| `/api/largest` | drop hidden files (and 404 a hidden `path`). |
| `/api/path-index`, `/v1/files`, `/v1/objects` | 403 (raw bytes can't be filtered). |
| `/api/plans/*`, `/api/actions`, `/api/db/*`, owners/estate/assignments | 403 (D1-derived path lists). |
| `/og/*` | a key whose grant is restricted gets the `anon` tier (shapes, no names). |
| `/data/*` | unchanged (dates, totals, top-level dirs only) — but 403 `age.json` if a hide prefix is a top-level dir. |

The client needs no changes to be safe; it renders a hidden dir as a leaf. Nice-to-have: mark it ("hidden from this link").

## Minting

The share-link console (`AdminPage.tsx`) gains a "hide paths" field (one prefix per line) that appends `<base>:hide:<prefix>` scopes to the mint.

## Phases

1. `hide` — `restrict.ts`, the route table above, tests per route (a restricted grant identity against fixture stores: exact responses), console field. Demo: `laptop:hide:Users/ryan/Downloads/2025 tax returns`.
2. `root` — same table with "outside the root" in place of "below a hide prefix", plus ancestors.

Verification 2026-10-11: a temporary read-only grant hiding the demo directory passed live API checks on production and dev. Its hidden-root subtree was aggregate-only; Largest files returned no hidden files; search excluded them; direct descendant requests returned 404; raw-index/admin path-list routes returned 403. Chrome on the dev fix also verified the hidden-folder view, parent navigation without the guest key and reload of the space-containing path. Both test grants were revoked and rejected subsequent authentication. The test found two viewer URL bugs (encoded path segments and a redeemed key returning on SPA navigation); fixes are deployed to production, with rollout verification described in [sub-daily captures]. Phase 2 remains unimplemented.

[sub-daily captures]: done/subdaily-captures.md
