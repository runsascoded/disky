# `@rdub/treemap`: canvas `var()` fills + `onDrawn` — round 2 from mgu

Follow-on to `specs/done/treemap-outlines-followups.md` (landed as `522fa2d`). Two more things mgu needed on its `packages/treemap` mirror (branch `gcs`); both are small and self-contained.

## 1. Canvas renderer paints nothing for `var()` fills

Same root cause as the outline overlay's §2: `parseColor` returns `null` for `var(--other)` / `var(--ink)`, so a consumer whose `colorForCell` names theme colors gets un-painted cells — in mgu, every "other" cell vanished into the container ground the moment the renderer switched to canvas. Fix on mgu's copy: new `src/cssColor.ts` exporting `cssColor(el, c)` (the resolver that was inlined in `OutlineOverlay`) and `colorResolver(el)`, a per-pass memoizing wrapper. `TreemapCanvas` sets a module-level `resolveVar = colorResolver(canvas)` at the top of each paint effect and `rgbaAt` resolves through it before `parseColor`; `OutlineOverlay` uses the same resolver. Both exported from the index. Rebuilding the resolver per pass is what makes a theme toggle repaint correctly.

## 2. `OutlineGroups.onDrawn?: (keys: string[]) => void`

Called after each overlay paint with the keys that produced ≥ 1 segment — the groups actually visible. mgu's legend keys only those fates (one red frame on screen → a one-entry key, not all three). Documented as cheap and idempotent per relayout; the consumer dedupes before setting state so it can't loop.

## Landing

mgu's copy is the reference (`packages/treemap/src/{cssColor.ts,TreemapCanvas.tsx,OutlineOverlay.tsx,outlines.ts,index.ts}`); diff against `main` after `522fa2d`.

### Landed (dt)

All four items ported to disk-tree's canonical `packages/treemap`:

1. **Canvas `var()` fills** — new `src/cssColor.ts` (`cssColor` + memoizing `colorResolver`). `TreemapCanvas` sets a module-level `resolveVar = colorResolver(cv)` per paint effect; `rgbaAt` resolves through it before `parseColor`. `OutlineOverlay`'s inline resolver is replaced by the shared one. **Also exported from `index.ts`** (`cssColor`, `colorResolver`) — the spec asked for it; mgu's own index didn't, but the public API is the right home.
2. **`OutlineGroups.onDrawn?`** — added to the interface; `OutlineOverlay` collects the keys that produced ≥1 segment and calls it after each paint.
3. **Stray hover-tip guard** — the `document` mousemove `useEffect` in `Treemap.tsx`, active only while an unpinned tip shows.
4. **Canvas↔DOM parity** — `TreemapCanvas` reads `--dt-treemap-lbl-fs`/`-sm` (px or rem) per pass and threads `inlineSizeMinWidth` (new `TreemapProps` field, default 90) through `PaintOpts`; the DOM label uses the same threshold.

**Deviations from mgu's mirror (applied only the four spec deltas):**
- `outlines.ts`'s seam-snap block differs cosmetically (mgu inlined the comment and renamed `iv→span`) — same fix already landed here as `522fa2d`, left as-is.
- mgu's unrelated `CellCtx.chain?` / `renderCellExtra` addition is **not** part of this spec — skipped.
- disk-tree's `nestedHues` (b771fac) is dt-only and untouched.

Tests: `tests/cssColor.test.ts` (6, exact-equality) + an `onDrawn` case in `OutlineOverlay.render.test.tsx`. Full suite 158/158, `tsc -b` clean (package + ui consumer). Items 1 (canvas) & 4 (label sizes) are canvas-paint behaviors jsdom can't render, and mgu already CIC-verified its copy; not re-CIC'd here.

## 3. Stray hover-tip guard (`Treemap.tsx`)

A hover tip occasionally outlived its cell in mgu (the pointer left by a path no `mouseleave` covered — a tip re-laid under the cursor, the window edge, the portal boundary) and then sat there until the next cell hover; clicking away did nothing because it wasn't pinned. mgu's copy adds a `document` `mousemove` listener, active only while an unpinned tip is showing, that clears the tip when the pointer is outside `.dt-treemap-cell / .dt-treemap-tip / .dt-treemap-map / .dt-treemap-canvas`. Pinned tips are untouched (the pin already clears on outside mousedown / Esc).

## 4. Canvas ↔ DOM parity: label sizes and the inline-size threshold

The canvas renderer hard-coded 13.5/11.5px labels and `w > 90` for the inline size, so it set type larger than a consumer's DOM styling and showed sizes the DOM hid. mgu's copy reads `--dt-treemap-lbl-fs` / `--dt-treemap-lbl-fs-sm` off the canvas element at the start of each paint (px or rem) and threads `inlineSizeMinWidth` into `PaintOpts`. Any other renderer-only constant should follow the same rule: one source, the consumer's.
