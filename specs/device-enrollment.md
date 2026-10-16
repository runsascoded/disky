# Connect a Mac: device enrollment for a user's disky backend

Requested by Ryan on 2026-10-16 (app session), with the M1 MacBook as the test patient: adding a host to disk.rbw.sh should be "install, grant Full Disk Access, sign in, click Connect", not a repo checkout, a venv, `~/.aws` keys, `.envrc` secrets and a Pulumi edit per Mac.

## Principles (Ryan)

- **One backend per user, shared by all their hosts.** m3 and the NAS already share one stack: `captures/<host>/…` → R2 event → queue → `capture-trigger` → Batch `ingest.sh`, with `HOST_STORES` mapping a host to its store. A new host piggybacks on it; no per-host Batch job, bucket or deploy.
- **Zero on-host disk footprint.** A connected host never builds its index locally: the app's capture streams layer-1 shards straight to R2 (as `scan.to: r2://…` does today), and the backend builds the path store. A tool needed when the disk is full must not fill it. (The local view's on-host index is the local-only mode's, unchanged.)
- **Everything ships in the app.** No Python, venv, direnv or repo on the host: what the drainer and the notify fallback do in Python today moves into the app (Rust).
- **Secrets live in the Keychain**, put there by the app, never in `disky.json`, a file, argv or chat.

## Today (what m3 is wired with), and what replaces it

| Piece | m3 today | Connected host |
|---|---|---|
| Identity | none (the bucket key is the identity) | a **device token** minted at enrollment, revocable per host |
| Upload | `scan.to: r2://disk-tree/captures`, `AWS_PROFILE=m3` (an R2 key in `~/.aws`) | **presigned PUTs** the site mints per shard for `captures/<host>/…` only: no R2 key on the host |
| Host → store | `HOST_STORES` (ingest job env) + `STORES_JSON` / `src/stores/nas.ts` (site build) | a `hosts` table in D1: host id → store key, label, mark; ingest and site read it, no redeploy |
| Unknown host | **ingested as the primary store** (`laptop`): a new host's scans would mix into m3's | **refused** (ingest fails loudly; nothing published) |
| Deletions | `wt/m3/aws/laptop-drain` (Python, a venv) polling D1 with a `CLOUDFLARE_API_TOKEN` (D1 write) | the app polls the site's API with its device token for its own host's runs, deletes via `backend_for`'s local path (to the Trash), reports bands back |
| Alerts | `direnv exec wt/m3 … disk-tree notify -f` (Pushover keys in `.envrc`) | Pushover user key + app token in a Settings field → Keychain; the app calls Pushover's API itself |
| File previews | Pulumi (`CloudflareTunnel`) + `disky tunnel set` + `disky tunnel token` | optional at enrollment: the site creates the host's tunnel, DNS route and Access service token via the CF API (server-side token) and returns the connector token; the app stores it |
| OS | ad-hoc: Open Anyway, notifications via Script Editor, FDA may need re-granting per update | Developer ID + notarization (Ryan): Gatekeeper, native notifications and a stable FDA identity; FDA itself stays a manual grant |

## Flow

1. The app is signed in to the site (the existing `disky://open?link=` hand-off).
2. Settings → "Connect this Mac to disk.rbw.sh" (admins only): the app sends `{host: <slug of the computer name, editable>, label, home: "Users/<user>", os, version}` (`home` is per host: the site's `~`); the site creates the `hosts` row (store key = host slug unless taken) and returns a device token (a grant scoped to `host:<id>`: upload, its own deletion runs, its own status). Optional "Allow file previews" makes the tunnel too.
3. Scheduled and "Scan now" captures upload through presigned URLs, minted per batch of shards (a capture writes dozens) with a short TTL under a fixed key prefix (`POST /api/hosts/<id>/uploads` → URLs for `captures/<host>/<root>/<stamp>/…`, then `_SUCCESS.json`). The device token is a grant, so the routes that refuse grants (`/v1/laptop-files`, the restricted ones) stay closed to it. The existing trigger → ingest runs unchanged, now resolving the store from `hosts`.
4. The site shows the host as a store (its label, mark, "laptop online", "next scan") without a deploy.
5. Disconnect: revoke the device token (and the tunnel); the store's history stays.

## Phases and owners

1. **Unknown host refused** (`ingest.sh`: `[local]`/m3) — before anything else, so a stray capture can't pollute the primary. **Done in code** ([local] 0c5a83a9, m3 env 23ef219c: `PRIMARY_HOST` names the primary, any other host not in `HOST_STORES` exits 3 before any record, R2 or D1 write). **Live since 2026-10-16** (aws `pulumi up`: image `6ea605ec`, job def `disk-tree-m3-ingest` rev 32, `PRIMARY_HOST=m3`, `HOST_STORES=nas=nas`; m3 3549cb1e); m3's 18:00 run is the first live check that m3's own captures still ingest. Until phase 2, any other host's capture is refused, not published.
2. **`hosts` table + store resolution from D1** (site + ingest; `[local]`), seeded with `m3 → laptop`, `nas → nas`; `HOST_STORES` / `STORES_JSON` become fallbacks, then go. Ingest reads `hosts` first (it has the D1 token). The site is the larger part: stores are build-time TS today (`src/stores/*.ts`, `STORES_JSON`), so a runtime registry (`/api/hosts`: path, label, mark, scheme, home) and `withStore` resolving unknown keys from D1. **Live since 2026-10-16** (design: m3 `specs/hosts-registry.md`): D1 `disk-tree-m3-db` has `hosts(id, store, label, home, root, mark, created_at, created_by, revoked_at)`, seeded `m3 → primary`, `nas → nas`; ingest image `8445a7c1` (job def rev 33) routes each capture with `dt-cloud hosts resolve <host>` (unknown or revoked → exit 3 before publishing; `PRIMARY_HOST` / `HOST_STORES` only apply without the table); the site serves every row as a store at `/<store>` with no deploy (`window.__DISKY_HOSTS__` for members, `GET /api/hosts`).
3. **Enrollment + device tokens + presigned uploads** (site `[local]`; app: the Connect button, Keychain, the upload client in `dt-capture`). Contract: m3 `specs/device-api.md`:
   - `POST /api/hosts` (admin session) `{host, label, home, os, version}` → `201 {id, store, token}`; re-enroll → `200` with a fresh token; the primary's id → 409.
   - The token is a `host:<id>` grant (Bearer, no expiry); a revoked token → 401.
   - `POST /api/hosts/<id>/uploads {root, stamp, keys}` → `{urls, expires}` (≤100 flat keys, 900 s TTL). Keys go to `captures/<id>/<root>/<stamp>/<key>`; `_SUCCESS.json` is uploaded last.
   - `GET /api/hosts/<id>` → the row + `seen_at` + `last_ingest`; `DELETE` disconnects.
   - App side: the token goes in Keychain `disky-device-token`; `disky.json` gets `connect: {site, id, store}`; the root slug is sanitized to `[A-Za-z0-9._-]{1,64}`.
   - Needs a prod D1 migration + a deploy (Ryan's go); m3 pings when it's on dev.
   - Site side built (m3 `51dedfbc`, 7 route tests, a real presigned PUT on a local stack). App side built against it, tested on a fake site, not yet against dev:
     - `dt-capture` `Target::Device` (`device.rs`): one sign + PUT per file in write order, so `_SUCCESS.json` goes last; retries; a 401 reads "disconnected".
     - App `connect.rs` + Settings → "Upload to the site". For enrollment, the app sends `POST /api/hosts` itself, with the webview's session cookie (`cookies_for_url`) and the site's `Origin`, so the token never passes through page script or a URL (m3's review). Also: status (`seen_at`, `last_ingest`), Open, Disconnect.
     - The scan job: an explicit `jobs.scan.to` wins (m3 unchanged). Otherwise a connected Mac uploads, and an unconfigured scan job no longer indexes locally.
4. **Drainer in the app** (app; site: a device-token runs API replacing the D1-token poll).
5. **Pushover in the app** (app: Settings field, Keychain, direct API call; `notify` command stays as an escape hatch).
6. **Tunnel at enrollment** (site: CF API with a server-side token; app: already consumes `files` + the Keychain token).
7. **Developer ID** (Ryan: Apple Developer Program, a "Developer ID Application" certificate, an App Store Connect API key for notarization → GitHub secrets; app: wire signing + `notarytool` into `macos-app.yml`).

The M1 is connected after phase 3 (it can scan + show up), and is the end-to-end test of each later phase.

## Later, not now

- **Bring your own cloud**: a user gives disky an R2 / S3 / GCS key and disky deploys this backend into their account (the IaC components are already deployment-generic).
- **Hosted**: disky runs the backend and a per-user silo in its own bucket, billed with a markup. Turnkey, but an operations and liability commitment; revisit once enrollment works for Ryan's own hosts.
- **Mobile**: Pushover covers phone alerts; a mobile-web view with Web Push (or native apps) can follow.
