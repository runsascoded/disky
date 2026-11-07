# `@disk-tree/react` Treemap: reconvergence deltas for the marin consumer

Written 2026-08-14 from a marin-gcs-usage session. marin is swapping its private
`site/src/Treemap.tsx` (415 L) for this package's generic `<Treemap<T>>`, which was factored
*from* it (`76aaa3b`, Item D.1). A component-diff found 7 upstream gaps; **the changes are
implemented in this working tree as uncommitted changes** (this session doesn't have commit
rights here) — review + commit from a disk-tree session, or tell the marin session to.
`pnpm typecheck` + all 21 vitest tests pass; `ui/` `tsc -b` clean (all additive).

## Changes (all in `packages/react/src/Treemap.tsx` + `index.ts`)

1. **Hover semantics port** (marin `b219f47`, postdates the factoring): leaf cells hover their
   whole body; **branch cells hover only their title-bar label** (`pointerEvents:'auto'` +
   `onMouseMove` on the label; none on the branch body); per-cell `onMouseLeave` removed —
   clearing moved to the `.dt-treemap-map` container. Fixes child→parent tooltip flicker when
   sweeping across a branch's children, keeps child→child transitions smooth.
2. **`colorForCell` gains a 4th arg** `ctx: CellCtx = { w, h, hasKids }` (new exported
   interface). `hasKids` = "renders nested tiles at current size" — consumers need it for
   container-neutral coloring and leaf-only hatch/dim treatments; it depends on render-time
   dims (`r.w>90 && r.h>44`) the data alone can't know. Backward-compatible (3-arg callbacks
   ignore it).
3. **`renderCrumbSuffix?(n, path)`** — replaces the hardcoded `— {formatSize(size)}` crumb
   suffix (marin adds object counts + $/mo). **`renderFooter?(n, path)`** — row below the map
   (marin's usage-hint footer).
4. **`chrome?: boolean`** (default true) hides the breadcrumbs/legend/fullscreen bar;
   **`showLabels?: boolean`** (default true) hides in-cell labels. Together ≈ marin's `redact`
   og:image render.
5. **Fold overhaul**: (a) folding now applies at **every** nesting level (was top-level only);
   (b) new **`mergeSmall?: (small: T[]) => T`** prop — consumer builds the folded stand-in as
   a first-class `T`, which then gets normal label/tooltip/click/color treatment (marin's
   folded nodes carry aggregated `tm`/`us`/`d` so tooltips stay real). Default (no
   `mergeSmall`) keeps the synthetic gray `(+n)` `FoldedNode` with suppressed interactions.
6. **Styling hooks**: label typography CSS-var-ified (`--dt-treemap-lbl-pad`,
   `--dt-treemap-lbl-fs`, `--dt-treemap-lbl-fs-sm`); cells get a `dust` class when
   sub-14px (so consumers can suppress box-shadows in CSS instead of inline); crumbs nav gets a `dt-treemap-crumbs` class.
7. **Nit**: rollup wrapper `<div>` only renders when `renderRollup` returns non-null (was an
   unconditional flex-gap-eating wrapper); same guard on the new footer.

## Suggested commit message

`Treemap: hover-semantics port + colorForCell ctx + crumb/footer slots + chrome/showLabels + first-class nested folds`

## Follow-ups (not done here)

- marin also wants to delete its local `useHoverPin`/`squarify` copies in favor of this
  package's exports (already exported; no upstream change needed).
- Consider porting marin's `.cell` inset-box-shadow/hover-brightness defaults into
  `styles.css` as opt-in classes.
