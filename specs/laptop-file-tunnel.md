# Laptop file previews through a Cloudflare Tunnel

Requested by Ryan in the m3 thread on 2026-10-14: when the laptop is online, the hosted site should be able to open (preview) a file from the laptop store, the way a bucket store opens an object through `/v1/files` or a public base. Deletions already reach the laptop (stage → `/staged` dispatch → the drainer, which polls D1 every 30 s, backing off to 150 s while idle); this spec is only about reading file bytes.

## Shape

```
browser ──▶ disk.rbw.sh  /v1/laptop-files/<path>   (Pages Function, viewer gate + restrictions)
                │  fetch https://<tunnel host>/files/<path>
                │  headers: CF-Access-Client-Id / -Secret (service token)
                ▼
        Cloudflare Tunnel (named, outbound-only from the laptop; Access app on its hostname)
                ▼
        laptop: cloudflared ──▶ dt-serve 127.0.0.1:7792  /files/<path>  (range reads, read-only)
```

- **Outbound only.** `cloudflared` on the laptop dials Cloudflare; no inbound port, no public listener, nothing on the tailnet exposed. The tunnel hostname is behind a Cloudflare Access application whose only policy is a service token (no browser login path), so the hostname answers nobody but the site's Function.
- **The edge route** (`site/functions/v1/laptop-files/[[path]].ts`): `withStore` + `requireViewer`, then the restrictions below, then a streamed `fetch` to the tunnel with the service token from Function secrets (`LAPTOP_TUNNEL_URL`, `LAPTOP_TUNNEL_CLIENT_ID`, `LAPTOP_TUNNEL_CLIENT_SECRET`). `Range` passes through (the leaf viewer range-reads); responses are `cache-control: private, no-store`. A tunnel error (laptop asleep, app not running) is a 503 with `{ offline: true }`, which the leaf viewer renders as "the laptop is offline — preview when it's awake", not an error.
- **`objectSource`** (`site/src/objects.ts`) gains a `tunnel` kind: a file store whose deployment advertises the route (the proxy info the client already fetches) opens objects through it. Bucket stores are unchanged.
- **The laptop side** (`apps/tauri/crates/dt-index/src/http.rs`, `dt-serve`): a `/files/<path>` route that serves a file's bytes (GET/HEAD, `Range`, `content-type` by extension, `content-length`), read-only. It accepts a request only when Access's `Cf-Access-Jwt-Assertion` header verifies against the Access app's audience (defense in depth: the loopback listener must not trust whatever reaches it), and only for a path under the configured scan root, after `realpath` (no `..`, no symlink escape). The app runs `cloudflared tunnel run` as a child (like the scan/drain jobs) when the setting is on, and reports its state in the menu.

## Restrictions (all enforced at the edge; the laptop re-checks the path root)

- **Who:** admins only to start (`requireAdmin`), widened later to viewers on purpose if Ryan wants. Never a share-link grant: `/v1/laptop-files` joins `RESTRICTED_BLOCKED`, and any `via: 'grant'` identity is refused even without hide scopes. File *contents* are a step beyond the metadata a guest link shows.
- **What:** never under a hidden prefix of any scope (reuse `restrict.ts` `isHidden`), never above the store root, and a deny-list for secrets that a size map shows but a preview must not: `~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.gnupg`, `~/Library/Keychains`, `.env*`, `.envrc`, `*.pem`, `*.key`, and the Chrome/Superhuman profile dirs. The deny-list is deployment config, not code defaults.
- **How much:** a size cap per response (e.g. 25 MiB for a whole-file GET; range reads of larger files allowed for media scrubbing), and a rate limit per session.
- **Audit:** each served path is logged to D1 (`access_log` already exists for viewers; add the path and bytes) so "who opened what" is answerable.

## Ownership (branches carry exactly their code)

| Piece | Branch | Why |
|---|---|---|
| Edge route, `objectSource` `tunnel` kind, offline UI, restrictions, tests | `local` `[local]` | Any local-machine deployment (this Mac, a NAS, someone else's laptop) would use it; bucket deployments wouldn't |
| `CloudflareTunnel` IaC component (tunnel, DNS route, Access app + service-token policy) in `infra/cf/` | `local` `[local]` | Same reason; a component with no names in it, like `CaptureTrigger` |
| The m3 instance: names in `infra/cf/Pulumi.rac.yaml`, the Function secrets, the deny-list | `m3` | This deployment's config |
| `dt-serve` `/files`, JWT check, `cloudflared` lifecycle, the menu setting | `tauri-native-app` (wt/app) | The app session owns that worktree |

## Phases

1. **App side behind a flag:** `dt-serve /files` with the path-root and JWT checks, unit tests (traversal, symlink escape, range, a missing or bad JWT). `cloudflared` run by hand.
2. **IaC + edge route on `local`:** the component and route with tests (admin only, grant refused, hidden prefix refused, deny-list, size cap, offline 503, range pass-through).
3. **m3 instance:** `pulumi up` on the cf stack (Ryan's go), Function secrets, a dev deploy and CIC: open a PDF and an `.mp4` under `~/Downloads` on dev.disk.rbw.sh, then with the laptop asleep (the offline message), then from a share link (refused).
4. **App lifecycle:** the app starts and stops `cloudflared` with the setting; prod.

## Decisions (Ryan, 2026-10-16)

- **Who:** admins only (`requireAdmin`). Opening it to allowlisted viewers is a later, deliberate change; share-link grants stay refused either way.
- **NAS:** stays metadata only for now. A `cloudflared` container on Unraid can follow once the laptop path has run for a while; the edge route's `tunnel` source is generic enough to point at a second origin.
