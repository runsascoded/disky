# Explanations leave the floating layer: the help line

**Why.** One `Tooltip` serves two jobs. *Data tips* on cells and rows (sizes, owners, mark provenance) are the content: anchored beside the pointer, they belong where they are. *Explanations* on controls — what "written" colours by, canvas vs DOM, cell gutters, the y-axis toggle, about 60 `<Tooltip>`s over `.has-tt` controls — fire on hover and on focus, cover the sibling control on menus and the tapped control on a phone, and a `<select>` that keeps focus after a choice left its tip pinned (9/17). A tip that gets in the way of the thing it explains is the wrong surface for the explanation.

**Interim (shipped with this spec):** tips open on keyboard focus only (`useFocus({ visibleOnly: true })`), the ⚙ menu's tips sit to the menu's left again (with the viewport `size()` cap so they no longer clip on a phone), and the lifecycle fold stops fetching snapshots for scans that predate the first recorded one.

## 1. The help line

A single fixed strip at the bottom of the viewport (above the SpeedDial on desktop; the SpeedDial itself expands into it on a phone) shows the explanation of whatever control is **hovered or keyboard-focused**, and is empty otherwise. Nothing floats over a control, so nothing covers the row below, the option list, or the tap target; long copy gets a full line (two on a phone) instead of a 420 px box; and a stuck-open state is impossible because the line has no open state — it mirrors hover/focus and clears on leave/blur.

- **`Help` component**: a context provider (`HelpProvider`, in Root) and a hook `useHelp()` → `{ text, setText }`. A **`<Explain text>` wrapper** replaces `<Tooltip>` on controls: it sets the line's text on `pointerenter` / `focusin` and clears it on `pointerleave` / `focusout`, keeps the `has-tt` underline (renamed `has-help`), and sets `aria-describedby` to a visually hidden element holding the same text, so screen readers keep the description.
- **Preference** `help` (`localStorage`, `?h=0|1` overrides): on by default; `?` (already the shortcuts modal) gets a sibling `h` binding and a SpeedDial button that toggles it. Off = the line is not rendered and `<Explain>` is inert apart from `aria-describedby`.
- **Content**: unchanged copy, moved verbatim. Where a tip currently embeds live values (the read-lens start date, the scan's byte floor) the wrapper takes a ReactNode as today.
- **Data tips stay `<Tooltip>`**: cells, table rows, legend items, mark dots, the class-mix table. The one rule: if the content is *about the thing under the pointer* it floats; if it is *about what a control does* it goes to the line.

## 2. Layout

`position: fixed; left: 0; right: 0; bottom: 0`, `padding: 6px 16px`, panel background with a top hairline, `font-size: 12.5px`, `min-height` one line so the page doesn't jump; the SpeedDial gains `bottom` offset equal to the line's height while it is on. On a phone the line is two lines max with `text-overflow` and a tap opens it as a bottom sheet. `body` gets matching `padding-bottom` so the last section isn't hidden.

## 3. Migration

One pass over `site/src`: each `<Tooltip>` wrapping a `.has-tt` control becomes `<Explain>`; the rest stay. The ⚙ menu, the colour/shade/scan selects, the y-axis and roots toggles, the diff window chips, the age chart's granularity and colour-by buttons, and the marks bar are the bulk. `Tooltip`'s `pinnable` mode is data-only and unchanged.

## As built (2026-09-17)

`site/src/Help.tsx` (`HelpProvider`, `Explain`, `HelpLine`) over the pure reducer in `helpState.ts` (hover and focus tracked apart; hover wins, blur falls back to hover). Preference `help` on/off in localStorage via `prefs.tsx`; `h` toggles it (use-kbd action) and a SpeedDial button mirrors it; no `?h=` URL override (a preference, not page state). Migrated: the bar's colour/shade selects and owner `not`/`×`, the path-filter clear, the diff's from-select, span chips, `≈ scope` and `largest changes`, the ⚙ menu rows, the legend metric chips, ⛶, the map's ⓘ (now focusable), `mark all` and the note input, the user menu's unit buttons, the mark feed's `in diff window`, and the chart's `fit / from 0` and `stacked / lines` toggles (which had native `title`s). Left floating: cell/row/legend-item/mark-dot/provenance/crumb-path/class-mix tips (data), the sweep pages (a later pass). The SpeedDial keeps its own floating tip. `body` gets `padding-bottom` while the line is mounted (`:has`).

## 4. Tests

- `site/src/help.test.ts`: the provider's reducer — enter/leave/focus/blur sequences leave the expected text (a leave after a focus keeps the focused text; blur clears it).
- CIC at desktop and phone width: hover the colour select (line shows, nothing floats), pick an option (line clears on leave, no pinned tip), ⚙ menu rows (line shows, both rows tappable), keyboard-tab through the bar (line follows focus).
