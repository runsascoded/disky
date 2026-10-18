# Chart annotations

Hand-written notes pinned to dates on the "Size over time" chart. Not generated: an admin writes each one.

## Why

cw.oa.dev/meta's chart drops from 8.8 TB to 813 GB on 10/13. That was a scope change (the `oa-gcs-usage-dvx` bucket left cw's meta store in the CW data isolation), not a deletion. Nothing in the data can say so; a note on the chart can.

## Schema (D1, migration `chart-annotations`, additive)

```sql
CREATE TABLE chart_annotations (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  store   TEXT NOT NULL,   -- 'primary', or a STORES_JSON key (secondary store or union view)
  at      TEXT NOT NULL,   -- a scan id or date: YYYY-MM-DD or YYYY-MM-DDTHHMM (UTC)
  text    TEXT NOT NULL,   -- 1..2000 chars, trimmed
  author  TEXT NOT NULL,   -- the admin who wrote or last edited it
  created INTEGER NOT NULL,  -- unix seconds, like the other tables' `ts`
  updated INTEGER NOT NULL
);
CREATE INDEX chart_annotations_store ON chart_annotations (store, at);
```

Every write also appends a row to `admin_edits` (`tbl = 'chart_annotations'`, pk = the id, old/new row JSON), as `/api/db` and the session-log switch do.

## API (`site/functions/api/annotations.ts`)

| | | |
|-|-|-|
| `GET /api/annotations[?store=K]` | `{ annotations: [{id, at, text, author, created, updated}], applied }` | viewer of the store |
| `POST /api/annotations[?store=K]` `{at, text}` | 201 `{ annotation }` | admin |
| `PATCH /api/annotations[?store=K]` `{id, at?, text?}` | `{ annotation }` (the editor becomes `author`) | admin |
| `DELETE /api/annotations?id=N[&store=K]` | `{ deleted }` | admin |

- The store is `store=` as every data request carries it (none = `primary`). A secondary store's notes need its own scope too (`STORES_JSON` `scope`, e.g. `/meta`'s `admin`), for reads and writes. An unknown store is a 404.
- Not edge-cached (`private, no-store`): it is one indexed D1 read, and an admin's write must show at once. No `RESPONSE_V` / `CACHE_V` bump (no cached body changed). The client caches it for 5 minutes (react-query) and refetches after a write.
- A D1 without the table (the migration not applied yet) answers `GET` with `{annotations: [], applied: false}`, not a 500. Writes are a 503 naming the migration. This is a deliberate exception to "no fallbacks for a missing index": the table is new, and each deployment applies migrations on its own schedule.

## UI

- `@disk-tree/react` `TimeSeries` takes `markers` (`{key, x, label}`) and `renderMarker`. A marker inside the plotted x-span reserves a 16 px lane above the plot. It draws a thin amber dashed line through the plot, and its label sits at the top of that lane, clear of the point callouts. `placeMarkers` (pure, tested) orders markers by x and drops any off the span. A label that would run past the right edge ends at its line; one that would overlap an earlier label becomes a small dot.
- `site/src/chartNotes.ts` (pure, tested): `notesInView` places each note at the chart's own x for `at`. A plotted scan's x is its start; any other date is UTC midnight. It keeps only notes inside the plotted span, so `1w` / `1m` hide older ones, and the `abs` / `Δ` / `%` modes never move a note. A note's label is its first line, cut to 28 characters.
- Each label is wrapped in the site's floating-ui `Tooltip` (no native `title`), showing the full text, its date, and the author's email local part (no addresses on public deployments). Light and dark colors are `--dt-ts-marker` / `--dt-ts-marker-ink` in `app.scss`.
- Admins (`useIsAdmin`, passed in as `SizeOverTime`'s `admin` prop) get:
  - a "+ note" button in the chart header. It opens a compact form whose date is prefilled with the diff window's end, else the latest plotted scan. It is disabled, with an explanation, when `applied` is false.
  - alt-click on a point, which opens the same form at that scan.
  - a pinnable tooltip with edit (the same form) and delete (a second click confirms).
- Non-admins see notes read-only.

## Turning it on per deployment

Apply the recipe to the deployment's D1 (`wrangler d1 execute <db> --remote --file site/migrations/chart-annotations.sql`), confirm with `dt-cloud d1-schema check -d <db>`, then `git rm` the recipe on that branch. Until then the chart shows no notes, and the "+ note" button is disabled.
