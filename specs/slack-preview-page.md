# `/admin/slack`: the digest OP, rendered and tunable on the site

**Status:** proposal, 2026-11-02. Shared (`[cloud]`), every deployment's site. Phases 1–2 are read-only. Phase 3 (saving a parameter set the live digest uses) adds a D1 table and needs Ryan's go before it starts.

## Goal

An admin page that shows a deployment's monthly digest OP (the text and the plot) exactly as the job posts it. It shows:
- what is live now: the current month thread's OP;
- the same OP re-rendered under other parameters, side by side, so a change like the rolling window (specs/digest-rolling-window.md) can be judged on the deployment's own data before it ships.

## The problem: two renderers

The job renders the OP **text** in Python (`digest_cw.op_body`, `digest_gcs.op_body`) and asks the site for the **plot** (`POST /api/digest-image`). A site page can draw the plot the same way, but "the text exactly as the job would" needs the job's renderer. Porting it to TS and keeping both would drift.

**Decision:** one renderer, the site's.
- The OP text moves to TS (`site/src/digest/`: `opText`, `imageRequest`, one module per template, `cw` and `gcs`; pure and DOM-free, so the page and the Functions both import it).
- The job sends the window's rows and gets back the text and the image request (`POST /api/digest-op`), as it already does for the image.
- Python keeps what is the job's: loading rows from the snapshots (`load_month`, senders and triggers), the Slack/Discord converge, state, permalinks, and the replies (they are posted once and never re-rendered, so they stay in Python).

## Data

**Rows** are the renderer's only input: each scan in the window and its baseline, i.e. what `Month` / `gcs.Month` hold today. Per scan:
- `scan`, `slug`, `trigger`;
- the primary's `tb`, `classes` and `extra` (cw); `tb`, `cost` and per-class TiB (gcs).

They are plain JSON (`DigestRows`, versioned). The cw/gcs `Scan` dataclasses serialize to it.

**Params** (`DigestParams`), each default being today's behaviour (`DEFAULT_PARAMS`; in the URL as `digest/codec`'s `w30,t0.7-1,dw,hmw,kw`):
- `window_days` (30): the window's length, inclusive of today;
- `today_at` ([0.7, 1.0]): where the latest scan sits across the width on the month's 1st and last day, linear between;
- `diff` (`window` | `mtd`): the "What changed" span;
- `headline` (subset of `mtd`, `window`, in order): the headline figures;
- `weeks` (`window` | `month`): which weeks the summaries list.

Params feed both the text (`opText(rows, params, cfg)`) and the image request (`imageRequest(rows, params, cfg)`). The card takes `today_at` through the request.

**The preview bundle:** each converge also writes `digest/preview/<channel>/<variant>/<YYYY-MM>.json` to the deployment's **served store** (`servedStore`: R2 under `SERVED_R2`, else the store bucket), holding:
- `rows`, `params` (the ones used), `cfg` (the digest config's display fields: title, site, units, buckets, quotas, prices);
- `op` (the posted text), `image` (the posted request);
- the thread's `op_ts`, its `permalink` and `posted_at`.

Writing it to the served store is needed because the converge state itself lives where the job keeps it: gcs on GCS (`gs://<data>/digest/`), hf, r2 and the demo on R2. The site reads only its served store, so no new bucket grants are needed.

## The page

`/admin/slack` (admin-only, `requireAdmin`; a lazy chunk like `/admin/usage`):
- **Month picker** (default: the current month), listing the months the bundles cover.
- **Live** column: the posted OP from the bundle, as Slack renders it (mrkdwn → HTML: bold, italics, links, `:arrow_degN:` as the arrow images, the image line as the PNG). The plot is redrawn from the bundle's `image` request. A link goes to the thread (`permalink`).
- **Candidate** columns (1–3, "+ compare"): the same rows under edited params, re-rendered client-side by the shared TS renderer (`opText`). The plot comes from `POST /api/digest-image` with `imageRequest(rows, params)`, so it costs one render per change, debounced. A column's controls are the params above, each with a "reset to live" control. Lines that differ from the live OP are marked.
- **Params in the URL** (`use-prms`: `?dk=<bundle key>&dc=<params>;<params>`, e.g. `?dc=.;w14,dm`), so a comparison is a link.
- **Rows inspector** (collapsed): the window's rows as a table, for checking a figure by hand.

New endpoints:
- `GET /api/digest-preview[?b=<bundle key>]` (admin): the bundles, or one (`b`, not `key`: the auth gate reads `?key=` as a share-link token).
- `POST /api/digest-op` (viewer grant, the job's): `{rows, params, cfg}` → `{text, image}`.

## Phases

1. **Shared renderer, read-only page.**
   - The TS renderer (`opText`, `imageRequest`) for both templates. **Parity:** the Python goldens' fixtures (`cloud/tests/fixtures/digest/`) gain a `rows.json` per case, and a vitest renders each and compares byte for byte with the Python dry-run's OP text and image request. Only then does anything switch.
   - The job writes the preview bundle; it is additive and posts nothing new.
   - `GET /api/digest-preview`, `/admin/slack` with live + candidate columns.
   - **Test** on the r2.rbw.sh demo (its digest's state and snapshots are already on R2; deployed from `cloud`/`dev` by `deploy-r2.yml`) and on gcs dev (dev.gcs.oa.dev, a month of real history; deployed by the gcs session). Not hf: under three weeks of history.
2. **The job renders through the site.** `dt-cloud digest` posts `rows` + `params` to `/api/digest-op`, and the Python `op_body` / `image_body` are deleted. The goldens then pin the job's calls, with the site's answers recorded. Per deployment, after its site ships phase 1. The job already requires the site token for the plot, so no new failure mode. **Done 2026-11-03**: every deployment rendered through the site (`renderer: site`) first; then the Python renderer and the `renderer` option went. The parity fixtures pin the job's side (`cfg`, `rows`, urls: `test_digest_parity.py`) and the site's answer (`op`, `image`: `parity.test.ts`); the Python goldens see a stand-in OP (conftest's `fake_site_render`).
3. **Saving params (needs Ryan's go: D1 table + migration).** See below.

## Phase 3: saved params (for Ryan)

- **Table** (`site/migrations/digest-params.sql`; `site/schema.sql` regenerated): `digest_params(id INTEGER PRIMARY KEY, channel TEXT, variant TEXT, params TEXT NOT NULL /* DigestParams JSON */, note TEXT, created_by TEXT NOT NULL, created_at INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 0)`. Append-only: saving inserts a row and moves `active` to it, in one batch. History is kept, so reverting is re-activating a row.
- **Endpoints:**
  - `POST /api/digest-params` (admin): `{params, note}` → the new active row.
  - `GET /api/digest-params` (viewer, the job's grant): the active row, or none (defaults).
- **The job** reads the active params at each converge (`dt-cloud digest`, before rendering) and records them in the bundle. Without a row, or with the site unreachable, it uses the defaults and logs it: a digest is never skipped over params.
- **Effect on threads:** a save changes the current month's OP from the next converge on (the OP is re-edited every scan). Replies are untouched, so a saved change never re-posts or deletes. The page says this beside the save button and shows the next scheduled converge.
- **Audit:** each save is an `auth_log`-style admin action (who, when, the diff vs the previous active row), listed on the page.
- **Recipe rollout:** the migration applies per deployment's D1 (`--file`), each on its own go. The page hides "Save" until the table exists (`/api/digest-params` → 404 `not migrated`).

## Template variants (cw's quota pressure)

`specs/digest-quota-pressure.md` (cw) builds on this renderer: quota pools sorted most-full first with 🔴/🟠 marks keyed to `alert_pct`, an OP plot of % of quota (one line per pool, a 100% rule), optionally a generated gauge-emoji set. Its seams here:
- **Pool series:** derived from the cw rows and config: each scan's primary `tb`/`classes` and `extra` (the other buckets), and each bucket's `quotas` and `zone` (`DigestCfg.buckets`; buckets sharing a zone summed into one pool, as `digest_cw._groups` does). cw adds a `pools(rows, cfg)` helper beside `cw.ts`, with no new job output.
- **Variant params:** new `DigestParams` fields (e.g. `marks: 'none' | 'circles' | 'gauges'`, `plot: 'usage' | 'pct' | 'pct+bars'`), each with a codec letter. Their defaults stay what the job posts until cw switches the job, so parity holds. The page's candidate columns show any field `Controls` lists.
- **Glyphs:** the clause renderer takes the mark per clause from the params (a `mark(fill, cfg)` hook); the page's `mrkdwn` renders `:emoji:` names it doesn't know as their text, so a gauge set needs its names mapped to images there.

## Open questions

- Should `cfg` (quotas, units) also be tunable on the page? Proposed: no. It is deployment config (`job/digest.yml`), not presentation.
- Discord's OP (gcs) renders from the same text through `discordify`. The live column could show both platforms; proposed: Slack only, Discord as a toggle.
