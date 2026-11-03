# `<Treemap>` per-cell brush styling — a border + fill for "selected"/"hovered" cells, in *both* tiling modes

> **Landed.** `CellStyle.ring` (`string | { color; width?; inset? }`) resolves via `resolveRing` in `cellStyle.ts` (default width 2px, `inset` default true). The DOM renderer stacks it first in the cell's `boxShadow` list (over the structural gutter, in either mode; follows `borderRadius`); the canvas renderer strokes a matching rounded-rect last in `paintCell`. It flows through the existing `colorForCell` / `lens` path — no new prop, no new event. Verified live in the playground across gaps/shared × DOM/canvas. Consumer bump (file-tree drops its forced-`shared` + `borderWidth` workaround, returns `ring` from `emphasize()`) is the next dist re-pin, alongside the already-landed `onCellHover`. (Since landed, the treemap moved to its own `@rdub/treemap` package — `packages/treemap/src/cellStyle.ts` — with `@disk-tree/react` re-exporting it; file-tree should re-pin `@rdub/treemap`.)

Companion to `specs/done/treemap-cell-hover.md` (the `onCellHover` output signal, already landed) and `~/c/js/file-tree/specs/tree-sources-and-treemap.md` (the file-tree side). Where `onCellHover` closed the *event* gap for bidirectional brushing, this closes the *styling* gap: giving a brushed/selected cell a legible, customizable **border** — not just a fill tint — regardless of tiling mode.

## The problem

file-tree's split view brushes the map from an externally-controlled path: a `lens` keyed to `highlightedPath`/`selectedPath` returns a `CellStyle` that emphasizes the matched cell. Fill (`bg`) and label (`ink`) recolor fine in either tiling mode. The **border** does not:

- `CellStyle.edge` — the only border hook — renders **only in `shared` tiling**. In `gaps` mode (`Treemap.tsx`, the cell `boxShadow`), every cell gets a fixed `'0 0 0 1px var(--dt-treemap-cell-border, transparent)'` and `style.edge` is **never consulted**. So an accent border is a *silent no-op* in gaps mode.
- Sparse, few-big-tile layouts are exactly the ones that stay in `gaps` (a dir with 2–3 large files). That's also exactly where a single hovered/selected file most wants a crisp frame — and where it can't get one.
- Even in `shared` mode, border **width** isn't per-cell: it's `borderWidth(depth, dims)`, a Treemap-level fn keyed on depth only. `edge` sets color, never width — so a brushed cell can't be ringed any thicker than the neutral gutters around it.

Net: to make a brush border visible at all today, file-tree has to force `tiling="shared"` globally **and** bump `borderWidth` — trading the (nicer, for sparse dirs) gap-gutter aesthetic away across the whole map just to frame one cell. That's the tail wagging the dog.

## The ask

A first-class **per-cell emphasis ring** in `CellStyle`, honored in **both** tiling modes, customizable in color and width, and independent of the structural gutter (`edge` / `borderWidth`):

```ts
/** An emphasis ring around this cell — for brushing / selection highlights.
 *  Unlike `edge` (the shared-mode gutter half-stroke, which `gaps` mode
 *  ignores), `ring` is honored in BOTH tiling modes and is painted as a
 *  box-shadow, so it never affects layout. `width` in px; `color` any CSS
 *  colour; `inset` draws it inside the cell box (default true) rather than
 *  outside. It follows the cell's corner radius and composites over whatever
 *  structural `edge`/gutter the cell already has. A bare string is shorthand
 *  for `{ color }` at the default width. */
ring?: string | { color: string; width?: number; inset?: boolean }
```

This flows through the **existing** `lens` / `colorForCell` path — no new top-level prop, no new event. A consumer already returning a `CellStyle` to emphasize a cell just adds `ring`. Fill (`bg`) and ink already recolor in both modes; `ring` is the missing border half. Together they cover exactly the user's ask: "changing colors and adding/customizing a border, in gap and gapless modes."

## Where it renders

- **gaps branch** — today the cell's `boxShadow` is `'0 0 0 1px var(--dt-treemap-cell-border, transparent)'`. Append the ring to that shadow list: `… , {inset?} 0 0 0 {width}px {color}`. Keep the existing 1px cell-border for structure; the ring stacks on top. gaps cells already have `borderRadius` 3 (1.5 for dust) — box-shadow follows the radius automatically, so the ring is rounded to match.
- **shared branch** — today `inset 0 0 0 {edge}px {style.edge ?? builtinEdge ?? …}`. Append the ring as an **additional** box-shadow, so a brushed cell can be ringed independently of — and thicker than — its shared half-stroke. This also removes the need for a consumer to inflate the global `borderWidth` just to widen one cell's emphasis.
- **canvas renderer** — paint the same ring in the canvas-backed path (a stroked rounded-rect on the cell box), so it survives the DOM→canvas migration in `specs/done/treemap-scaling.md`. A brushed cell is always a drillable-spine/individual cell, not folded dust, so this is a per-cell stroke, not a hit-tested region.

## Why a dedicated `ring`, not "honor `edge` in gaps too"

`edge` is *semantically* the shared-tiling gutter: each neighbor paints **half** of every shared boundary, and the two halves meet to form one stroke (that's why `edgeContrast` derives per-cell luminance-matched colors). Overloading it to *also* mean "a full ring around the cell in gaps mode" conflates two different things — a shared boundary vs. a standalone emphasis outline — and would make `edgeContrast`'s half-stroke logic ambiguous. A separate `ring` is a clean, mode-independent emphasis primitive; `edge` stays the structural gutter it already is.

## Consumer side (what file-tree does once this lands)

- **Drop the workarounds**: remove the forced `tiling="shared"` and the `borderWidth` bump; return the map to default `gaps` (keep the gap gutters).
- `emphasize()` returns `ring: { color: accent, width: N }` alongside the existing `bg` fill mix + `ink`, for the hovered cell (white) and the selected cell (blue, thicker). Border becomes the primary cue, fill the secondary — at any tile density, in either mode.
- Bump the `@disk-tree/react` dist pin to include **this** + the already-landed `onCellHover` (`78f1fc2`), and wire the remaining `onHoverPath` (map→table hover) in the same integration pass — so bidirectional brushing and the border land together.

## Not in scope

- **No selection *state* in DT.** "Selected" vs. "hovered" stays file-tree's distinction — it owns `selectedPath` (via `onCellClick`) and `highlightedPath` (via the listing / `onCellHover`), and expresses each as a different `ring`/`bg` in the `lens`. DT just needs to *paint* whatever ring the lens returns.
- **No animation/transition** of the ring — a consumer can add CSS if it wants one.
- **No Treemap-level `ringWidth` default** required — per-cell `width` (with a sensible built-in fallback) suffices. Add a default only if it turns out several consumers want to set width once.

## Live context

Verified in file-tree (2026-09-02) against the current pin `0c486606` (`0.1.0-dist.e758294`, the same one the `onCellHover` note references): a `lens` returning `CellStyle.edge` rings a cell in `shared` tiling but is dropped entirely in `gaps` (the default for sparse layouts). Forcing `tiling="shared"` makes the border appear but flattens the gap-gutter aesthetic across the whole map — which is the motivation for a mode-independent `ring` instead.
