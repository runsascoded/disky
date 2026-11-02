/**
 * GET /api/digest-preview → { bundles: [{key, month, channel, variant}] }       admin
 * GET /api/digest-preview?key=digest/preview/<channel>/<variant>/<YYYY-MM>.json → the bundle
 *
 * The digest's preview bundles (specs/slack-preview-page.md): each Slack converge writes the month's OP as posted,
 * with the rows and config it was rendered from (`digest.preview_bundle`), to the served store under
 * `digest/preview/` (the job's `preview` config). `/admin/slack` shows one and re-renders its rows under other
 * params (`src/digest`). Read through the served seam (`servedStore`), as `/api/usage` reads its snapshots. Asking
 * for a bundle of another version is a 404.
 */
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'
import { servedStore } from '../_lib/index.js'

export const PREVIEW_PREFIX = 'digest/preview/'
const KEY_RE = /^digest\/preview\/([^/]+)\/([^/]+)\/(\d{4}-\d{2})\.json$/

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  const store = servedStore(ctx.env, [PREVIEW_PREFIX])
  const key = new URL(ctx.request.url).searchParams.get('key')
  if (key !== null) {
    if (!KEY_RE.test(key)) return json({ error: 'key: digest/preview/<channel>/<variant>/<YYYY-MM>.json' }, 400)
    let doc
    try {
      doc = JSON.parse(new TextDecoder().decode((await store.get(key)).bytes))
    } catch (e) {
      // a month without a bundle (no converge since `preview` was set) is a 404; anything else is an error
      if ((e as Error).name !== 'NotFoundError') throw e
      doc = null
    }
    if (!doc || doc.version !== 1) return json({ error: `no preview bundle ${key}` }, 404)
    return json(doc, 200, { 'cache-control': 'private, max-age=60' })
  }
  const bundles: { key: string; month: string; channel: string; variant: string }[] = []
  // the store lists one level (`/`-delimited): walk `<channel>/<variant>/` down to the bundles
  const dirs = [PREVIEW_PREFIX]
  for (let dir = dirs.shift(); dir !== undefined; dir = dirs.shift()) {
    let cursor: string | undefined
    do {
      const page = await store.list(dir, { cursor })
      for (const e of page.entries) {
        const m = KEY_RE.exec(e.key)
        if (e.isDir) dirs.push(e.key)
        else if (m) bundles.push({ key: e.key, channel: m[1], variant: m[2], month: m[3] })
      }
      cursor = page.cursor
    } while (cursor)
  }
  // newest month first, then by channel and variant
  bundles.sort((a, b) => b.month.localeCompare(a.month) || a.channel.localeCompare(b.channel) || a.variant.localeCompare(b.variant))
  return json({ bundles }, 200, { 'cache-control': 'private, max-age=60' })
}
