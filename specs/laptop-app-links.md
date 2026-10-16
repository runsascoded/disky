# Laptop status and `disky://` actions on the site

Requested by Ryan on 2026-10-16 (app session): after file previews landed (`laptop-file-tunnel.md`), the site should show whether the laptop is reachable, and be able to hand a file to the macOS app ("Show in Finder", "Open on this Mac"), toward a better Finder with Finder itself one click away. The app side is done on `tauri-native-app`; this spec is the site side.

## What the app now offers

**Health through the tunnel.** `GET /files/` (empty path) on the tunnel, Access-signed like any file, answers `200 {"ok": true, "root": "/"}`. When the app is off or `disky tunnel off`, the tunnel 530s (or the edge's existing offline path applies), as for a file.

**`disky://` links** (`apps/tauri/src-tauri/src/links.rs`), beside the existing sign-in hand-off `disky://open?link=…`:

| Link | Does |
|---|---|
| `disky://view?url=<page URL>` | opens that page in the app's window; only https pages of prod / dev (or the app's own `http://127.0.0.1:7792`), switching the app's site to match |
| `disky://reveal?path=<path>` | selects the item in a Finder window (runs nothing) |
| `disky://open-file?path=<path>` | opens it in its default app: documents / images / audio / video / plain folders at once; anything that can run code (apps, packages, scripts, installers, HTML, unknown types; judged on the symlink-resolved target) only after a native dialog whose default is "Show in Finder" |

`path` is the store path as the site has it (`Users/ryan/Downloads/x.jpg`, the laptop store's root being `/`), `encodeURIComponent`'d; `~/…` and absolute paths work too. A missing path is refused (logged in the app's `disky.log`).

The contract: any web page can navigate to a `disky://` link, so the app never trusts the caller; the safety is in the app (nothing runs code without its own confirmation). The site needs no auth for these links: they act on the machine the clicker sits at, and only if it has disky installed.

## Site work (`[local]`, wherever the laptop store's UI lives)

1. **Status route** `GET /v1/laptop-files/status` (admin, like `/v1/laptop-files`): fetch the tunnel's `/files/` with the service token, short timeout (~3 s); `200` + `ok` → `{ online: true }`, anything else → `{ online: false, reason }` (530 / timeout / the 404 `not served locally` body). `cache-control: private, max-age=15` so a page of rows doesn't stampede it.
2. **Indicator**: for admins on a store with the tunnel route, a dot + "laptop online" / "laptop offline" beside "scanned 4h ago" in the header, from (1), refetched on focus and every ~60 s. Tooltip: previews reach this Mac through the disky app's tunnel; offline = asleep, the app not running, or previews turned off in it.
3. **Leaf-viewer actions**: in the file viewer (and the row's context / hover actions), "Show in Finder" → `disky://reveal?path=…`, "Open on this Mac" → `disky://open-file?path=…`. Show them to admins on the laptop store when either the page is inside the app (user agent contains ` disky/`, as the "Open in disky" button already checks) or always, with a tooltip "needs the disky app on this Mac". Ryan's call; the app session's suggestion is both (in-app plain; in a browser with the tooltip). Directories get "Show in Finder" too.
4. **"Open this page in disky"** for a signed-in browser whose app is already signed in: `disky://view?url=<location.href>` (the existing "Open in disky" mints a sign-in link instead; keep it for the not-yet-signed-in case, or offer both).

## Status (2026-10-16)

Site side done as `[local]` 9d5b55dd and live on dev. `/v1/laptop-files/status` gates like a file and isn't audited, since it's polled. `LaptopIndicator` sits beside "scanned …". `AppActions` puts "Show in Finder" and "Open on this Mac" in the object panel, and "Show in Finder" on the view card's directory. Both show on a Mac only, inside the app plainly, and in a browser with a "needs the disky app" note (both, per the app session's suggestion). "Open this page in disky" is in the user menu.

Verified on dev against the installed app (11f007f4): the status probe through the tunnel reports `{ online: true }`; with `disky tunnel off` it reports `{ online: false, reason: "tunnel 530" }` and the header shows "laptop offline". The links carry the encoded store path. Clicking them isn't automated, because Chrome's "Open disky?" dialog blocks automation; the app session verified each link on the Mac. Prod is waiting on a deploy.

**Next scan** (Ryan, via the app session): the health JSON also carries `schedule` (local HH:MM, sorted; `[]` while scheduled scans are off) and `tz` (from /etc/localtime). `/v1/laptop-files/status` passes them through, type-checked, and the header reads "scanned 5h ago · next 18:00", computed by `nextScan` in the app's zone and shown in the viewer's. Verified live on dev 2026-10-16 (`[local]` 791eedec).

## Not in scope

- Any write action through the tunnel (it stays read-only; deletion keeps going through staging → the drainer).
- Opening for non-admins; share-link grants never see these actions.
