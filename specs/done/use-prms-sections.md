# Retire `hashSpy` → `use-prms/sections`

**Status:** done (2026-10-19), phases 1 and 2. **Source:** use-prms ships the section spy upstream (the `use-prms/sections` subpath), ported from this repo's former `site/src/hashSpy.ts`. The upstream spec (full API and rationale) is `use-prms/specs/done/section-spy.md`.

## Why

`hashSpy.ts` (~300 lines; home page scroll-spy, staged-deletions page) was lifted into use-prms verbatim-then-generalized; a second copy here was pure drift risk. The upstream version adds **param mode** (the section in the query instead of the fragment), which a dOGI can see — directly relevant to [`section-aware-dogi`] (the Worker reads the section param in [`routes.ts`] `pageView`).

## Dependency

`site/package.json` pins a use-prms dist build, `661e994` (built from use-prms `fe9a288`), which includes `use-prms/sections`. No bump was needed. use-prms `HEAD` (`4e1f32f`) has no `src/sections` changes since.

## Phase 1: fragment mode, no behavior change

- `hashSpy.ts` / `hashSpy.test.ts` deleted (the tests live upstream).
- [`SectionAnchor.tsx`] kept as a thin wrapper: upstream `SectionLink` with the `sec-anchor` class.
- [`App.tsx`] and [`StagedPage.tsx`] call `useSectionSpy` (`ids`→`sections`, `legacy`→`aliases`, `offset`→`reference.offset`, `spy`→`write: 'replace' | 'off'` from the `scroll-spy` pref, `location`/`navType` under `nav`).
- Each page's `mode` is a **module constant**, not an inline literal: upstream memoizes its mode resolution on `opts.mode`'s identity, and its scroll-listener effect depends on the resolved mode, so a fresh `{ kind: … }` per render re-binds the listener every render.
- Saved positions moved from `hashSpy.pos` to `use-prms:sections:pos` (lost once per tab).

## Phase 2: param mode on the home page

### The key is `section`, not `s`

The upstream default key `s` is taken on both pages: the home page's `?s=` is the shade axis (`?s=c`, `shadeCodec`), and the staged page's `?s=` is its table sort. The spy would have overwritten them. The key is `section` ([`homeSections.ts`] `SECTION_KEY`), which is also what the dOGI already read and what share links already carried (`?section=diff`). So old links keep working with no special case, and `pageView` needed no new alias. Switching to a shorter key (e.g. `sec`) would mean changing `SECTION_KEY` and adding a legacy `section`→`sec` rewrite on load, since a page loaded from an old `?section=diff` link would otherwise keep that param after the reader scrolls away.

### Changes

- [`homeSections.ts`] (DOM-free, shared with the Functions): `SECTION_KEY`, `SECTION_IDS`, `LEGACY_ANCHORS`, `sectionOfHash`.
- [`App.tsx`]: `mode: { kind: 'param', key: 'section' }`. Default `adoptFragment` turns an incoming `#diff` / `#changes` / `#size-over-time` into `?section=diff` / `?section=over-time` and drops the fragment. Other params (`?s=c`, …) are preserved.
- `drillTo` and `openObject` build their navigations from `withoutSection(search)`: the router's `search` holds the last *linked* section, not the one in view.
- [`StagedPage.tsx`] **stays in fragment mode**. Its batch permalinks are plain `href="#batch-N"` links, the linked batch is unfolded from the router's `hash` (`StagedPage.tsx` ~L270–280), and the page has no per-batch card. Param mode would drop the fragment those rely on. `.stage-batch:target` in `app.scss` therefore still applies; it is the only `:target` selector in the site.
- [`shareSection.ts`] (share links, `og/tokens.ts` `mint`, `api/og` scope check): a fragment naming a home section is promoted to `?section=<id>` and the fragment dropped, which is the URL the page itself settles on. Any other fragment stays, and an existing `?section=` stays. Minted links are now `…?d=…&section=diff&og=<token>` (no trailing `#diff`).
- [`routes.ts`] `pageView` and `og/serve.ts` `cardData` use `SECTION_KEY`. Tests ([`routes.test.ts`]) pin that `?section=diff` (alongside `?s=c`) gets the diff card, other sections get the map card, and `?s=diff` is just a shade value.

### For the over-time card (follow-up, [`section-aware-dogi`])

`pageView` would need `sp.get(SECTION_KEY) === 'over-time'` → a new `OgKind` (`'series'`), with params `path` + the `MAP_KEYS` the series follows (`d`, `f`, `qs`, `o`, `by`, `cl`; `c` and the series' own view toggles if the card draws them). Then a `cardData` case, `serve.ts`'s `pu.searchParams.set(SECTION_KEY, …)` generalized from `kind === 'diff'`, and a title suffix (e.g. `· size over time`). The client already writes `?section=over-time`.

## Verified

- `pnpm exec vitest run` (218 files, 2526 tests), `tsc -b --noEmit`, `tsc -p functions --noEmit`, `pnpm build`.
- In Chrome, against `API_ORIGIN=https://r2.rbw.sh pnpm dev`:
  - `/#diff`, `/?s=c#changes` and `/#size-over-time` land and become `?section=diff`, `?s=c&section=diff` and `?section=over-time`.
  - Reader scrolling writes `section=tree-map|tbl|over-time|diff`, and clears it at the top.
  - A drill drops the section and starts at the top. Back and forward restore the exact `scrollY` (560 / 400).
  - The scroll-spy pref off stops URL writes and keeps saving positions.
  - The Diff heading link is `?section=diff`; clicking it parks there and marks `data-section-target`.
- The staged page wasn't exercised in Chrome: the r2 demo has nothing staged.

## Known upstream issue: the spy names a section early near the page bottom

The spy names `diff` while "Size over time" sits ~245 px below the top bar. The cause is upstream `sectionInView`'s bottom slide, not disky's configuration. Diagnosis and proposed fix: `use-prms/specs/section-spy-bottom-slide.md`.

[`section-aware-dogi`]: ../section-aware-dogi.md
[`routes.ts`]: ../../site/functions/_lib/og/routes.ts
[`routes.test.ts`]: ../../site/functions/_lib/og/routes.test.ts
[`SectionAnchor.tsx`]: ../../site/src/SectionAnchor.tsx
[`App.tsx`]: ../../site/src/App.tsx
[`StagedPage.tsx`]: ../../site/src/StagedPage.tsx
[`homeSections.ts`]: ../../site/src/homeSections.ts
[`shareSection.ts`]: ../../site/src/shareSection.ts
