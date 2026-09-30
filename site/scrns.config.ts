import type { Config } from 'scrns'

// The two headline treemap crops we share in #oa-marin — top-of-page header +
// full treemap + footer, framed as the viewport (scrns forces device-scale 1
// and screenshots the viewport, no clip — so width/height *is* the crop).
//
// `.treemap … .dt-treemap-cell` only exists once the (async, ~6.5MB) tree.json has been
// fetched and squarify-laid-out, so it gates the capture; the short settle
// covers the final layout pass. Run against the local dev server (no CF
// Access) — `pnpm dev` then `pnpm shots`.
const base = {
  width: 1600,
  selector: '.treemap .dt-treemap-map .dt-treemap-cell',
  preScreenshotSleep: 800,
}

const config: Config = {
  engine: 'puppeteer',
  host: 3253,
  output: 'screenshots',
  screenshots: {
    // Per-view height: the user legend wraps to an extra row (11 users vs 6
    // teams), so its header is ~40px taller and its footer sits lower. Each
    // height ends just below that view's "click to drill in…" footer, before
    // the age chart.
    'gcs-groups': { ...base, height: 912, query: '' },         // color by group (default)
    'gcs-users': { ...base, height: 966, query: '?c=user' },  // color by user
    // Public og:image: the redacted `/og` route (group treemap, no labels/$/
    // names) framed at the standard 1200×630 OG card. `.og … .dt-treemap-cell` gates on
    // the (async) tree.json layout, same as the crops above.
    'og': { ...base, width: 1200, height: 630, selector: '.og .dt-treemap-map .dt-treemap-cell', query: 'og', preScreenshotSleep: 1200 },
  },
}

export default config
