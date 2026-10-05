# `?d=YYMMDD` on every scan-scoped page

**Goal:** any page that renders data from *one* scan should accept `?d=YYMMDD`
(the same short scan id the home map uses) and show the same scan picker, so a
scan pin is a first-class, shareable, page-independent dimension — not a
home-map-only trick. Deep-linking `/users?d=260815` should show the 8/15 rollup;
switching the picker on `/user/will-held` should stay on that user at the new
scan.

## Today (the asymmetry to remove)

- **`App` (home map)** owns the whole scan machinery: `useUrlState('d', {encode:
  encodeScan, decode: decodeScan})`, a polling `scans.json` query, prefix-match
  (`dMatches[0] ?? scans[0]`), the `fmtScan` labels, and the `<select>` + the
  multi-match disambiguation strip. All of it lives in `App.tsx`.
- **`UserPage` / `UsersPage`** have their *own* `useLatestScan` = `scansQ.data?.[0]
  ?? null` — **always newest, no `?d`**. A `/users?d=…` link is silently ignored;
  there's no picker.
- **`MarksPage`** is the D1 actions ledger — **not** a single scan (actions
  reference the scan the actor was viewing, but the feed spans scans). `?d` does
  not apply; leave as-is. (A future "as of" filter would be a *different* param.)
- **`FilesPage`** browses the raw store by **path** (`/files/listing/<date>/…`) —
  the date is already in the URL path, not `?d`. Leave as-is.
- **`/og`, `/users/og`, `/user/:id/og`** render fixed snapshots; they may accept
  `?d` for parity but default to newest (OG cards are usually "latest").

So the real work is: **App, UserPage, UsersPage** share one scan hook + one
picker; MarksPage and FilesPage are explicitly out (documented, not forgotten).

## Design

### 1. One scan hook (`useScan`)

Extract the home map's scan logic into `src/scan.ts`:

```ts
export function useScan(store: Store) {
  const [dP, setDP] = useUrlState('d', { encode: encodeScan, decode: decodeScan })
  const scansQ = useQuery<string[]>({ queryKey: ['scans', store.key], queryFn: …, refetchInterval: … })
  const scans = scansQ.data ?? []
  const dMatches = useMemo(() => (dP ? scans.filter(s => s.startsWith(dP)) : []), [dP, scans])
  const asof = dMatches[0] ?? scans[0] ?? null
  return { asof, scans, dMatches, dP, setDP, pin: (s?: string) => setDP(encodeScan(s)), scansQ }
}
```

`encodeScan` / `decodeScan` / `fmtScan` move to `scan.ts` too (App re-imports).
This is a pure lift — App's behavior is unchanged, it just consumes the hook.

### 2. One picker (`<ScanPicker>`)

The `<select>` + disambiguation strip + `fmtScan` labels become
`src/ScanPicker.tsx`, driven by `{ asof, scans, dMatches, pin }`. App drops its
inline copy for `<ScanPicker/>`; UserPage/UsersPage add it.

### 3. Placement — shared chrome vs. per-page

`SiteNav` already renders on every non-home page and is the natural home for the
picker. **But** `SiteNav` today is store/scan-agnostic (it only needs identity).
Two options:

- **(A) Picker in `SiteNav`** — pass `{scan}` into `SiteNav` from each page that
  has one; render the picker when present, omit on MarksPage/FilesPage. One
  visual home for "which scan am I looking at", matches the "standard nav on
  every page" ask. *Cost:* `SiteNav`'s prop surface grows; the home map keeps its
  own inline picker (it leads with the `<h1>` + store switcher, `inline` mode).
- **(B) Picker per page**, near each page's title. Less coupling, but the picker
  sits in a different place on each page.

**Recommend (A)** — it's exactly the "top-nav chrome shared across pages" the
review asked for, and keeps one mental model. The home map stays special (its
picker is inline in the title row, alongside object counts + cost) — that's fine;
`SiteNav` is `inline` there and renders no picker, App renders its own.

### 4. Per-page wiring

- **UserPage / UsersPage:** replace `useLatestScan` with `useScan`; thread `asof`
  into `useScanFile` / `useMarkTotals` (both already take an `asof`); pass `scan`
  to `SiteNav`. `/user/:id?d=…` and `/users?d=…` then resolve to that scan, and
  the picker re-fetches the rollup in place.
- **App:** consume `useScan` (no behavior change); `SiteNav inline` renders no
  picker.
- **MarksPage / FilesPage:** no `scan` prop → `SiteNav` renders no picker.
  Document why in a one-line comment at each call site.

### 5. Edge cases (inherited from App, keep them)

- **Prefix match:** `?d=2608` → newest 8/xx. `?d=260815` → the 8/15 scan; several
  same-day scans → newest + disambiguation strip.
- **No `?d`:** newest, and the tab keeps *following* new scans (polling) rather
  than pinning whatever day it was opened — `?d` appears only on explicit pick.
- **Unknown `?d`:** no match → fall back to newest (don't 404 a shared link when a
  scan has aged out); optionally a small "scan 260701 not found — showing latest".

## Out of scope / non-goals

- MarksPage "as of" filtering (different param, different semantics).
- FilesPage — date already in the path.
- Cross-store scan lists (each store keeps its own `scans.json`; the hook is
  already keyed by `store.key`).

## Acceptance

- `/users?d=260815`, `/user/will-held?d=260815` render the 8/15 rollup; the
  picker shows 8/15 selected; changing it updates data + URL in place.
- Home map behavior byte-for-byte unchanged (pure refactor).
- MarksPage/FilesPage unchanged, with a comment noting they're intentionally not
  `?d`-scoped.
