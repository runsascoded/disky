# Treemap: export the current view as an image (copy / download)

**From:** marin-gcs-usage (cw-s3), 2026-09-16. During a quota incident a teammate clipped the treemap by screenshot and pasted it into Slack; a one-click "copy PNG" / "download PNG" would have been the natural affordance, and every deployment of this treemap (gcs, cw-s3, the planned R2/S3/GCS demos) wants the same thing.

**Status (dt, 2026-09-16):** implemented in `@rdub/treemap` — copy/download buttons (inline-SVG icons, `renderTip` tooltip slot) + `⌘/Ctrl+Shift+C`, `onExport`, `filename`, **title** composition, and per-button click feedback. **Renderer-independent** (offscreen render under the DOM renderer). **Legend composition is deferred** (a design fork — see "Legend" below). Consumed by dt's `ScanDetails` treemap. Verified in-browser under both renderers (canvas: live-canvas read; DOM: offscreen render) — the map composites under a title bar into a valid `image/png`, buttons flash a check on success.

## Why it's cheap

The cells and labels are canvas-rendered (`packages/treemap/src/TreemapCanvas.tsx`), so the pixels already exist: `canvas.toBlob('image/png')` → `navigator.clipboard.write([new ClipboardItem({'image/png': blob})])` for copy, or an object URL on an `<a download>` for download. Retina: the canvas is already drawn at `devicePixelRatio`, so the export is crisp without re-rendering.

## Proposal (`@rdub/treemap`) — as built

- `Treemap` prop `exportable?: boolean | ExportOptions<T>`, where `ExportOptions = { filename?: (ctx: { node; path }) => string; title?: boolean }`. `true` ≡ `{}` (bare map). When set, the crumb bar gets two small inline-SVG icon buttons (copy, download) before the fullscreen toggle; keyboard `⌘/Ctrl+Shift+C` copies while focus is inside the treemap.
- **Tooltips**: the buttons carry a native `title` by default (the core stays 0-dependency — it doesn't bundle a tooltip lib). A consumer passes `renderTip?: (label, button) => ReactNode` to wrap them in its own tooltip (MUI `<Tooltip>`, `@floating-ui/react`, Radix, …); when set, the native `title` is suppressed so tips aren't doubled. Applies to the copy/download/fullscreen buttons. (dt passes MUI `<Tooltip>`; the fullscreen/fold buttons ride the same slot.)
- **Renderer-independent.** Under the canvas renderer the export reads the live map `<canvas>` directly (`canvas.toBlob('image/png')`). Under the DOM renderer (the default) there's no map canvas, so the export **renders the same placed cells to an offscreen canvas** with the identical paint machinery (`renderMapToCanvas`, theme vars resolved against the in-DOM map element) — so the buttons show and work in both modes, and DOM mode pays the layout+paint cost only when a button is actually clicked. Either way: no DOM-to-image dependency, crisp on retina (drawn at `devicePixelRatio`). The base export is the map alone (what the teammate clipped).
- **Click feedback**: on a successful copy/download the button briefly (1.6 s) swaps its icon to a check, tints it success-green, and updates its label to `Copied ✓` / `Saved ✓` (so a `renderTip` tooltip reflects it live). A failed clipboard write falls back to a download and reports `Saved ✓`. `onExport`'s `kind` is the *actual* outcome (`copy` or `download`).
- **Title** (`title: true`): the crumb text — `path.map(getLabel).join('/') — <total>` — composited as a line above the map on an offscreen canvas, in the map's current theme colours (`getComputedStyle` on the map element for ink + ground; falls back to `document.body`, then the container constant), with a small margin.
- `filename` default: `<view-basename>-<YYYYMMDD-HHMM>.png` (`defaultExportFilename`; basename = the current view's last path segment, filename-sanitised). Consumers can derive from their scan id via the `{ node, path }` ctx.
- `onExport?: (blob, { kind: 'copy' | 'download' })` for analytics/toasts; the consumer shows any "copied ✓" affordance.
- Clipboard write (`copyPng`) needs a user gesture + secure context — both hold for a button click on HTTPS (and `localhost`); returns `false` (→ the caller falls back to `downloadPng`) when `ClipboardItem`/`clipboard.write` is unavailable or rejects (Firefox behind a flag, denied permission).
- Helpers are also exported standalone (`composeExport`, `canvasToPngBlob`, `copyPng`, `downloadPng`, `defaultExportFilename`) for a consumer that wants to wire its own affordance.

## Legend — deferred (design fork)

The proposal's `legend: true` (composite the "colour key") assumed the legend is drawable core data. It isn't: `renderLegend` is a **consumer ReactNode**, and the two deployments disagree on what it holds — dt's is a *controls panel* (gaps / dom·canvas toggles), which is nonsensical to bake into a PNG; a colour-key legend (mgu) is a different thing. Rendering the consumer's node into the PNG would need DOM-to-image (which the spec forbids) *and* would capture the wrong content for dt.

The honest options, pending a decision:
- **(a)** Core derives a **structured colour key** from the current level's top-level children (`label → categoricalStyle` slot colour) and draws it — well-defined at the root, murkier once `nestedHues` shades deeper levels.
- **(b)** Consumer supplies a **drawable** legend: `legend?: (ctx) => { label: string; color: string }[]`, composited the same way as the title. Keeps the core honest; each deployment supplies its real key.
- **(c)** Leave the legend to the screenshot; ship title-only.

Recommendation: **(b)** if a legend is wanted (mgu passes its key; dt passes nothing). Not built until we pick one — `exportable` deliberately omits `legend` for now so no caller wires a no-op.

## Consumer side

- **dt** (`ScanDetails`): `exportable={{ title: true }}` + `renderTip={(l, b) => <Tooltip title={l}>{b}</Tooltip>}` (MUI) on the main treemap. Works under both renderers (dt defaults to DOM). dt has no colour-key legend (its bar's right slot is a controls panel), so title-only is the whole story here.
- **cw-s3/gcs**: pass `exportable={{ title: true }}`; add `legend` once the fork above is resolved. Optional later: a "share" that uploads the PNG to the deployment's public icons Pages project and posts the URL to Slack — out of scope here.
