# Device API: enrollment, device tokens, presigned uploads (device enrollment phase 3)

The site half of phase 3 of `specs/device-enrollment.md` (on `tauri-native-app`), on top of the `hosts` registry (`specs/hosts-registry.md`, live 2026-10-16). The app (disky) is the only client: its Connect button, Keychain (service `disky-device-token`) and upload client are built against this contract.

## Auth

- **Enrollment** is an admin's browser session (the app's signed-in webview), same-origin only (`Origin` must equal the site's), never a grant.
- **A device token** is an `@open-athena/auth` grant: scopes `["host:<id>"]` only, `name: "disky device <id>"`, created by the enrolling admin, no expiry, used as `Authorization: Bearer <token>` (never redeemed). It passes no other gate (every viewer route wants the base scope; `/v1/laptop-files` and the admin routes refuse grants), so a leaked token can upload captures for its own host and read its own status, nothing else. Its grant id sits on the host row; revoking the grant (or the row) is disconnecting.
- A revoked token is a 401 everywhere: the app shows "Disconnected" and stops. No refresh flow.

## Routes

`<id>` is the host id (= its store key): `^[a-z0-9][a-z0-9-]{0,31}$`, not a reserved route (`_lib/hosts.ts` `RESERVED_KEYS`), not a build-time store key.

### `POST /api/hosts` — enroll (admin session, same-origin)

```json
→ {"host": "m1", "label": "M1", "home": "Users/ryan", "os": "macOS 27.0", "version": "0.9.3"}
← 201 {"id": "m1", "store": "m1", "token": "<device token, shown once>"}
```

- A new id creates the row (`created_by` = the admin's email).
- An id that exists (live or disconnected) **re-enrolls** it: the old token's grant is revoked, a new one minted, `revoked_at` cleared, label/home/os/version updated; same `id`/`store`, history kept. (Reinstalling the app on the same Mac.) `200` instead of `201`.
- The primary's id (`store = primary`) is refused (`409`): the deployment's own Mac uploads with its own keys. So is a row registered without enrollment (no grant: the seeded `nas`, whose captures come over m3's keys), and an id that is already a `STORES_JSON` store.
- `400` on a bad id / label (1–64 chars) / home (`Users/<name>`-style relative path, ≤ 256 chars); `403` for a non-admin or a grant; `403` cross-origin.

### `GET /api/hosts/<id>` — status (its device token, or an admin session)

```json
← {"id": "m1", "store": "m1", "label": "M1", "home": "Users/ryan", "os": "macOS 27.0", "version": "0.9.3",
   "created_at": "2026-10-16T18:30:00Z", "seen_at": "2026-10-16T20:01:12Z", "revoked": false,
   "last_ingest": {"scan": "2026-10-16T2000", "status": "succeeded" | "failed" | "running", "at": "2026-10-16T20:14:03Z", "error": null} | null}
```

`last_ingest` is the host store's newest `scan_runs` row (kind `ingest`). A capture the ingest refused never reaches `scan_runs` (it can't, for an enrolled host: refusal means unknown or revoked). Every device-token call stamps `seen_at`.

### `POST /api/hosts/<id>/uploads` — a batch of presigned PUTs (its device token)

```json
→ {"root": "root", "stamp": "2026-10-16T20-00-03Z", "keys": ["shard-00000.parquet", "shard-00001.parquet"]}
← {"urls": {"shard-00000.parquet": "https://<acct>.r2.cloudflarestorage.com/disk-tree/captures/m1/root/2026-10-16T20-00-03Z/shard-00000.parquet?X-Amz-…"},
   "expires": "2026-10-16T20:15:03Z"}
```

- Keys land at `captures/<id>/<root>/<stamp>/<key>`: `root` `^[A-Za-z0-9._-]{1,64}$`, `stamp` `%Y-%m-%dT%H-%M-%SZ` within ±1 day of now, each key `^[A-Za-z0-9._-]{1,128}$` (flat: shards + `_SUCCESS.json`), **≤ 100 keys per batch**, **TTL 900 s**. Ask for the next batch as the capture streams; a URL can't be refreshed, just ask again for the same key.
- Upload `_SUCCESS.json` last, after every shard's PUT succeeded: its `ObjectCreated` event starts the ingest (R2 notification → queue → `capture-trigger` → Batch), which routes by `hosts` and refuses a revoked host.
- PUT with no extra headers (the URL signs `host` only); any `Content-Type` is fine.

### `DELETE /api/hosts/<id>` — disconnect (its device token, or an admin session)

Revokes the grant, sets `revoked_at`. The store's history stays served; a later capture is refused. `{"revoked": true}`. An admin can't disconnect a row that was never enrolled (`409`).

### Later (phase 4)

`GET /api/hosts/<id>/runs`, `POST /api/hosts/<id>/runs/<run>` with the same token (the drainer in the app).

## Schema

`hosts` gains `grant_id TEXT`, `os TEXT`, `version TEXT`, `seen_at TEXT` (`site/migrations/hosts-device.sql`; a prod D1 migration, Ryan's go).

## Presigning

`_lib/presign.ts`: SigV4 query-string signing (WebCrypto, no dependency) over the store's own endpoint and keys (`STORE_ENDPOINT`, `STORE_ACCESS_KEY_ID` / `STORE_SECRET_ACCESS_KEY`: m3's "disky m3 laptop" token, Object R&W on `disk-tree`), region `auto`, path-style, `UNSIGNED-PAYLOAD`. Tested against AWS's published presigned-URL example.

## Status

Implemented (`[local]`): `_lib/presign.ts` (+ AWS's example vector), `_lib/devices.ts` (`enroll`, `hostStatus`, `disconnect`, `uploads`) + 7 tests, routes `api/hosts/index.ts` (GET list, POST enroll), `api/hosts/[id].ts` (GET, DELETE), `api/hosts/[id]/uploads.ts`, migration `site/migrations/hosts-device.sql`. Verified on a local stack (built site, fresh local D1): the browser's admin session enrolled `zz-probe` (201), its token read its status (`seen_at` stamped) and got a 900 s upload batch; the same presigner PUT a probe into the real `disk-tree` bucket (200), a presigned GET read it back (200, same bytes), and the probe was deleted. **Live 2026-10-16**: `hosts-device.sql` applied to `disk-tree-m3-db`; dev and prod deployed. Verified on dev from a signed-in admin browser: enroll `zz-probe` 201 (re-enroll 200), its token read its own status (200) and got an upload batch (200); with `cache: no-store`, the token got 403 from `/api/store`, `/api/hosts` (list, enroll), `/api/token`, `/v1/laptop-files/{status,get}`, `/data/{laptop,nas}/scans.json`, `/api/union-scans`, `/api/hosts/nas`; DELETE disconnected it (200) and the token then read 401. The probe row was deleted afterwards (grants left revoked).

The app's root slug maps anything outside `[A-Za-z0-9._-]` to `-` (cap 64): `<root>` is only a key segment; the ingest takes the store from the host and paths from the listing.
