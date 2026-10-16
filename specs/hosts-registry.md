# A `hosts` table: stores without a deploy (device enrollment phase 2)

The site half of phase 2 of `specs/device-enrollment.md` (on `tauri-native-app`). A connected Mac is a **host**; each host is a store the site serves and the ingest publishes into, registered in D1 rather than in build-time TS (`src/stores/*.ts`), `STORES_JSON` / `STORES_EXTRA` (wrangler) and `HOST_STORES` / `PRIMARY_HOST` (the ingest job's env).

## What a host store is

A capture host's store is always the same shape as the NAS's today: the primary's bucket and credentials, its own subdir (`snapshots/<store>/`, `listing/<store>/…`), `file:///` paths, D1 rows under `index_schema.store = <store>`, deletions on its own drainer (`executor: laptop`), no ownership ledger, no static name index (yet). So a row carries only what varies:

```sql
CREATE TABLE hosts (
  id TEXT PRIMARY KEY,            -- capture host (`captures/<id>/…`): the computer-name slug
  store TEXT NOT NULL UNIQUE,     -- 'primary' (the deployment's own) or a store key (`snapshots/<store>/`, `?store=`)
  label TEXT NOT NULL,            -- picker / title ("M1", "NAS")
  home TEXT,                      -- this host's `~` under its root (`Users/ryan`); NULL = no fold
  root TEXT,                      -- the scan root when paths keep the full path (`mnt/user`); NULL = `/`
  mark TEXT,                      -- tab icon key (`public/icons/`); NULL = the deployment's
  created_at TEXT NOT NULL,
  created_by TEXT,                -- the enrolling admin's email (NULL for seeded rows)
  revoked_at TEXT                 -- disconnected: the ingest refuses it; the site still serves its history
);
```

Seeded `m3 → primary` and `nas → nas` (m3's rows; the `[local]` recipe `site/migrations/hosts.sql` creates the table only). A store key is `^[a-z0-9][a-z0-9-]*$`, never `primary` for a non-primary host, and never a reserved app route (`api`, `data`, `v1`, `auth`, `staged`, `scans`, …: `RESERVED_KEYS`), since the store mounts at `/<key>`.

## Edge (`functions/`)

- `_lib/hosts.ts`: `loadHosts(env)` reads the table (one query, cached per isolate for 60 s; a missing table, i.e. an unmigrated D1, reads as no hosts), `hostStoreConfig(env, row)` → a `StoreConfig` (the NAS's vars: `ROOT_LABEL = label`, `SNAPSHOTS_SUBDIR = store`, `STORE_SCHEME = file:///`, `STORE_BUCKETS = *`, the primary's `STORE_BUCKET` / `STORE_REGION`, secrets mapped to the primary's own `STORE_ENDPOINT` / keys).
- `_middleware.ts` awaits `loadHosts` before `next()` (cached, so ≤1 D1 read per isolate per minute); `secondaryStores(env)` returns `STORES_JSON`'s stores plus each non-primary, non-revoked host's config not already there (an explicit `STORES_JSON` entry wins: it may carry more vars). `withStore` stays synchronous, so its 15 callers are unchanged.
- `GET /api/hosts` (viewers): `[{id, store, label, home, root, mark, revoked}]`.
- The page: for a member's session (the base scope; not a share-link grant), the middleware stamps `<script>window.__DISKY_HOSTS__=[…]</script>` at the top of `index.html`'s `<head>` (`pageHosts`, both the og-stamped page and the anonymous-shell branch), `cache-control: private, no-store`, so the client knows its stores before its first render (routes exist at boot, no loading flash). Anyone else's page is as built: host labels are private.

## Client (`src/stores.ts`)

`STORES` = the build's stores (`VITE_STORE`, `VITE_STORES_EXTRA`) plus a `Store` per stamped host whose key isn't already one: `hostStore(row)` is `nas.ts`'s shape with the row's label / home / root / mark, `path: /<store>`, `base: /data/<store>`. A build-time module for the same key wins (`nas.ts` keeps its copy). The primary row only matters to the ingest.

## Ingest (`infra/aws/ingest.sh`)

`dt-cloud hosts resolve <host>` (D1 over the HTTP API, as `scan-run` writes) prints `<store>` for a live row, exits 3 for a revoked or unknown one, and 4 when D1 has no `hosts` table. The ingest maps `primary` to `$STORE` (`laptop`) / D1 `primary`, else `<store>` / `<store>`. `PRIMARY_HOST` / `HOST_STORES` stay as the fallback only for exit 4 (an unmigrated D1); once every database has the table they go.

## Not in this phase

- Enrollment (`POST /api/hosts`), device tokens, presigned uploads: phase 3.
- A static name index per host (search on a host store reads `scan-not-indexed`, as the NAS does): a gap, after phase 3.
- Dropping `src/stores/nas.ts` and the `STORES_JSON` nas entry: once the hosts path has served the NAS for a while.

## Status

Implemented (`[local]`): the table (`site/migrations/hosts.sql`, `schema.sql`), `_lib/hosts.ts` + tests, the middleware's load and page stamp, `GET /api/hosts`, `src/stores.ts` `hostStore` / `withHosts` + tests, `dt-cloud hosts resolve` + tests, `ingest.sh` (17 ingest tests: D1 routing, refusal of an unknown / revoked host, a failed lookup fails, the env fallback without the table). Verified on a local stack (the built site under `wrangler pages dev`, a fresh local D1 from `schema.sql` with `m3`/`nas`/`m1` rows): `/m1` mounts "disk-tree — M1", `?store=m1` resolves as the NAS does, `?store=m2` is a 404, `/api/hosts` lists all three. Live once the migration and the m3 seed are applied to `disk-tree-m3-db` (Ryan's go), the site deployed, and the ingest image rebuilt (aws `pulumi up`).
