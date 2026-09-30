# Dynamic Open Graph cards (per-path treemap unfurls)

Status: **feature landed** (2026-09-19); the R2-write blocker is **resolved** (2026-09-20, §5) — rescans write again and the full HCCS `crashes` corpus is now the demo's crashes dataset.

Make every shareable disk-tree link unfurl with a treemap of *that* path, so pasting `https://r2.rbw.sh/r2/ctbk` (or a drilled `…/r2/ctbk/avail-v3`) into Slack/Twitter/Discord shows the bucket's actual space breakdown, not one static card. Applies to the public serverless demo (`disk-tree-demo` → r2.rbw.sh); the gated `disk-tree` project inherits the same code.

## 1. Why it needs server work

Crawlers don't run JS. The SPA's og tags are static in `index.html`, so every route unfurled identically — and worse, the original `og:image` pointed at `/og.png`, which never existed, so the card was a broken thumbnail. Two fixes: (a) a real default card, and (b) rewrite the tags per request path on the server, since that's the only thing a crawler sees.

## 2. Architecture

**`ui/functions/_middleware.ts`** — for a scan-route *document* (`/r2/<bucket>[/…]`, `/file/…`; not `/`, `/access`, `/staged`, `/s3`, `/compare/*`), pipe the SPA HTML through `HTMLRewriter` and set `og:title` / `og:image` / `og:url` / `twitter:*` from the path. `og:image` → `${origin}/og/<key>` where `<key>` is the path key (`r2/ctbk/avail-v3`). Pure path↔uri↔title mapping in **`ui/cfn/og.ts`** (`ogRoute`, `keyToUri`), tested in `cfn/tests/og.test.ts`. The `/api/*` gate branch is unchanged.

**`ui/functions/og/[[path]].ts`** — serves the card image, resolving in order:

1. **R2 `og/<key>.jpg`** — refreshed daily from the live treemap by `rescan-demo.yml` (tier A, freshest, tracks re-scans).
2. **static `/_og/<key>.jpg`** — seed cards bundled with the deploy (tier A, day-one). NB a *missing* static asset resolves to the SPA shell (200 `text/html`), not a 404, so the seed branch trusts the response only when its `content-type` is an image.
3. **edge render** — `squarify` the covering scan's depth-1 children → SVG → PNG (tier B, any uri). Guarded: any failure falls through to (4).
4. **static `/og.jpg`** — the site-default card.

## 3. Tier A — pre-rendered bucket cards

Pixel-perfect (real renderer, fonts, dust): captured from the live treemap by headless Chromium.

- **Seeds** (`ui/public/_og/r2/{ctbk,nj-crashes,jc-taxes}.jpg`, 1200×630) bundle with the deploy so bucket links work immediately.
- **Daily refresh**: `rescan-demo.yml` runs `ui/scripts/og-capture.mjs` (Puppeteer, installed on the fly so a normal `pnpm install` doesn't pull Chromium) after re-scanning each bucket, and uploads to R2 `og/r2/<bucket>.jpg`. `continue-on-error` — a capture flake never fails the scan run. **Currently blocked, see §5.**

## 4. Tier B — edge-rendered subtree cards

Any drilled path renders on demand at the edge, reusing the live widget's layout so it matches:

- **`ui/cfn/ogSvg.ts`** — `treemapCardSvg({uri, children, total, itemCount})` → SVG, using the pure `squarify` and a local `slotColor` mirror (parity with the package pinned by `cfn/tests/ogSvg.test.ts`). Header = wordmark + uri + `<size> · <n> items`.
- **`ui/cfn/ogRender.ts`** — `svgToPng` via resvg-wasm.
- `ogRender.test.ts` renders a real 1200×630 PNG end-to-end (Node reads the wasm bytes) and writes `tmp/og-b-sample.png` for inspection.
- Cost: ~1.7–5.3 s cold render, then CF edge-cached (`max-age=3600`).

### Edge gotchas (see memory `cfn-edge-wasm-and-dom-free`)

- **cfn is DOM-free** (`tsconfig.cfn.json` = ES2022 + workers-types). Importing the `@rdub/treemap` barrel drags the React/DOM graph into that compilation (~46 spurious errors). Fix: import only the pure `@rdub/treemap/squarify` (new subpath export); mirror `slotColor` locally (its module type-imports `CellStyle` from the DOM-heavy `Treemap.tsx`). Tests are excluded from `tsconfig.cfn`, so they *can* import the barrel for the parity assertion.
- **Workers can't compile wasm from bytes at runtime** (`CompileError: Wasm code generation disallowed by embedder`). `initWasm(fetch(url))` fails; the wasm must be imported as a build-time-compiled module. Vendored at `ui/cfn/vendor/resvg.wasm` + `cfn/wasm.d.ts` + `import wasm from '…/resvg.wasm'`. Node (tests/CI) *can* compile from bytes.
- **Fonts**: resvg wants ttf/otf (not woff2), served as static assets (`ui/public/_fonts/Inter-{400,600}.ttf`, fetched at runtime — that's allowed, unlike wasm). Static Inter TTFs come from the Google Fonts css2 API with an old User-Agent.

## 5. RESOLVED — R2 write token + cross-account credential split (2026-09-20)

`rescan-demo.yml` had failed **every run since ~2026-09-09**: `PermissionError: Access Denied` on `PutObject` to `disk-tree-demo` — the single R2 credential was read-only. Fixed by a least-privilege, **cross-account** credential design (CLAUDE.md "Cross-account credentials", `blobfs.py`):

- **`disk-tree-demo RW`** (RAC, Object R&W, `disk-tree-demo` only) → profile `rac-rw`, writes the demo bucket. This is the actual unblock.
- **`hccs …RO`** (HCCS, Object Read) → profile `hccs-ro`, reads the public source buckets `crashes` + `ctbk`, which have **moved to the HCCS account**.
- **`disk-tree-demo RO`** (RAC, Object Read) → profile `rac-ro`, reads the one remaining RAC source `jc-taxes`. *(Pending: the `R2_RO_*` keypair on hand authenticates but grants nothing — roll that token's S3 creds. jc-taxes stays stale until then; it is the only bucket affected.)*

`buckets.yml` maps each bucket → `endpoint_url` + `profile`, so one `index --to` run authenticates a HCCS source and the RAC target with different keys (`_s3fs(endpoint, profile)`, `S3Backend(profile=…)`). **`DISK_TREE_R2_ENDPOINT_URL` must stay unset** — it globally overrides the per-bucket endpoints and collapses the split.

CI secrets: `R2_RW_*`, `R2_HCCS_RO_*` (added 2026-09-20); the workflow writes an `AWS_SHARED_CREDENTIALS_FILE` with the two profiles + an inline `buckets.yml`. Verified 2026-09-20: `r2://crashes` (7,966 objects, 72.4 GiB) scanned from HCCS → published to `disk-tree-demo` on RAC → live at r2.rbw.sh (listed, drillable, `/og/r2/crashes` edge-renders). Memory: `rescan-demo-r2-write-denied` (resolved).

## 6. Also landed alongside (README / site OG)

- README: prominent clickable live-demo treemap hero + refreshed `screenshots/treemap.png` (was the retired plotly renderer) (`0ea42b0`).
- Site default OG: real `ui/public/og.jpg` (1200×628 ctbk treemap crop) + absolute `og:image`/`og:url`/`twitter:card` in `index.html` — fixes the broken r2.rbw.sh unfurl (`b39ebfa`).

## 7. Commits

`0ea42b0` README hero · `b39ebfa` site og.jpg + meta · `d42f389` tier A (middleware + seeds) · `703b3f7` tier-A daily refresh CI · `153ab98` tier B (edge render).

## 8. Possible follow-ups

- Tier B **write-through to R2** via the Function's R2 binding (bindings can write even with a read-only *API* token), so a cold sub-path render is cached durably rather than only at the CF edge — reduces repeat cold renders and survives isolate churn.
- **`nj-crashes` retirement.** The demo now carries both the old RAC `nj-crashes` (5.0 G, stale) and the new HCCS `crashes` (72.4 G). `nj-crashes` is dropped from the rescan loop; its stale scan can be deleted from `disk-tree-demo/scans/` once confirmed (a `delete`, so left for an explicit go).
- **jc-taxes RO.** Rejoin it to the rescan loop once the `rac-ro` token has a working keypair (§5).
