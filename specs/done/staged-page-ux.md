# Staged page: long paths, per-item delete, sizes

From the `~/.disk` cleanup session (2026-09-24), after Ryan reviewed an 18-item local plan (`~/Downloads` installers and redundant zips) on his phone at `m3:7791/staged` (side Vite → `disk-tree-server` with `DISK_TREE_DELETE_APPROVAL=staged`).

## 1. Long paths break the table on mobile

One long URI (`…/Insta360_Studio_6.0.4_release_insta360(RC_build71)_20260904_180546_signed_1788517496623.zip`) widens the whole table, so every row scrolls horizontally and the `unstage` column lands off-screen (the first screenshot showed only `unstage` links and an empty URI column). `StagedPage.tsx` has `wordBreak: 'break-all'` on the `<code>`, but the table still overflows.

Proposal:
- **Display relative to the plan's common prefix** (a header line reads `/Users/ryan/Downloads/`; rows show `ChatGPT.dmg`). Most plans are one or two directories.
- **Middle-elide** what remains (`Insta360_Studio_6.0.4_rel…1788517496623.zip`: keep the head and the extension-bearing tail) with the full URI in a floating-ui tooltip on hover / long-press, plus copy-on-click.
- `table-layout: fixed` with the path column taking the remaining width, so the action column is always visible.

## 2. Per-item delete ("delete now") on each row

Ryan wants to go down the list deleting one at a time rather than dispatching the whole plan. Today only `POST /api/dispatch` exists, and it's whole-plan and immediate (`for_real` defaults true; the UI's confirm step says `confirm — delete 18`).

Proposal: `POST /api/dispatch` accepts an optional `uris: [...]` subset → deletes just those, recorded as a normal `deletion_run` (so `staged` history and `undo` bookkeeping stay uniform), and the items leave the plan. UI: a per-row `delete` next to `unstage`, with the same inline two-tap confirm pattern as plan dispatch (`delete` → `confirm`).

**Executor note:** this is a local-server capability. A CFN (Worker/Lambda) can't reach laptop paths, so for `file://`-scheme items the executor is always the local server/CLI; the CP6–8 CFN/Batch axis applies only to bucket URIs. On a deployment without a local executor, the per-row control should hide for local URIs rather than 501.

## 3. Show each item's size and a plan total

`size_fn` already exists in `staged_backend.py`. Show the size per row and a total in the plan header (`18 items — 2.05 GiB`), so a dispatch confirm states what it frees. For local items, apparent size overstates what reflinked/cloned data frees (see `reclaim`); a "measure" action calling the `reclaim` engine for the plan would make the confirm honest, but it's optional here.

## 4. Dry-run affordance

The CLI's `dispatch` is dry by default; the UI's is not. A `preview` (dry dispatch: `for_real: false`) showing per-item bytes/objects before the confirm would match the CLI.

## Landed (2026-09-25, `cloud` `3938f3d`)

1. Paths show relative to the plan's shared dir (`under …/Downloads/`; `commonDir` backs off to a segment boundary and never swallows an item), middle-elided past 48 chars (`elideMiddle`, head + extension-bearing tail), full URI in a tooltip (hover / long-press, `enterTouchDelay=0`) and copied on click; `table-layout: fixed` with the path column taking the remainder and wrapping (the global `code` is `nowrap`; overridden for `.staged-path`), so size + actions stay on-screen at 390px. Helpers in `ui/src/staged.ts`, specs in `staged.test.ts`.
2. `POST /api/dispatch {uris}` dispatches a subset: `staged.dispatch(…, uris=…)` deletes just those, removes them from the plan, closes it only once nothing remains; an unstaged URI → 400 before anything runs. Per-row `delete → confirm` on the page, Flask peer only (`caps.static === false`); the edge keeps whole-plan enqueue. CLI: `disk-tree dispatch -u URI…`.
3. `GET /api/staged` items are `{uri, bytes, objects}` (edge: `null`s). `size_fn` now uses the freshest *covering* scan (was: only a scan of the exact URI, so a plan staged from a parent's scan sized as all zeros — your 18 items would have). Size column + plan total in the header and on the dispatch confirm. The `reclaim`-based "measure" stays optional/unbuilt.
4. `preview` runs a dry dispatch; the API reports the run's band totals (`bytes`/`objects`) so the alert says what a dispatch would free.

## Landed (2026-09-25, follow-up): multi-select, details links, Finder reveal

Ryan (from the 21-item queue): "i want kbd/modifier/mouse multi-select (like we have in other tables …)" and "can i click these files to see details and/or open them in Finder?"

- Each plan's table is a `useRowSelection` table (the scan-details primitive from use-kbd): click / ⇧-click range / ⌘-click toggle, row and header checkboxes, `j`/`k` + arrows, ⇧ extends, ⌘A, Esc. The keyboard layer registers once (first plan; use-kbd bindings are page-global), other plans get the mouse layer. The selection's count + total sits in the plan header (no layout shift on select) with `delete selected` (→ `confirm — delete N (size)`, Flask peer only: `dispatch {uris}`), `unstage selected`, `clear`.
- A row's path links to its details page (`uriToPath`: a dir → its listing + treemap, a file → the hex/text preview); the copy-on-click moved to a copy glyph after the path (full URI still in the tooltip).
- `GET /api/staged` items carry `kind` (`file` | `dir` | `null` — `staged_backend.describe` reads it from the covering scan's row; a scan root counts as a dir; the edge has no scans → `null`). The row's kind icon is the Finder-reveal button for local paths when the server can (`caps.reveal`, `POST /api/reveal`), like the scan-details icon column. `describe` also clamps a file to one object regardless of its scan's `n_desc` convention.
- Styles: `.scan-details-table, .staged-table` share the `.sel`/`.cur` rules + `user-select: none`; the two new narrow columns keep the 390px layout intact (verified: table right edge == container).
