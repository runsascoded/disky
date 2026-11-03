/** The digest's OP, rendered by the site (specs/slack-preview-page.md phase 2): the job posts a month's rows and its
 * digest config's display fields (`digest.cfg_json`) with the params it posts under, and gets back the OP's text and
 * its plot's `/api/digest-image` request, from the same renderer `/admin/slack` previews with (`src/digest`).
 *
 *   POST /api/digest-op {month: 'YYYY-MM', rows, cfg, params?: '<codec>', plot_url?, prev_url?}
 *     → {text, image}
 *
 * `params` is `src/digest/codec`'s string (`''` or absent: the defaults), read strictly: a field the page's lenient
 * decoder would drop is a 400, never a silent default. Viewer-gated (the job's grant); pure, nothing stored. */
import { type Ctx, json, requireViewer } from '../_lib/auth.js'
import { parseParams } from '../../src/digest/codec.js'
import { type DigestCfg, type DigestRows, imageRequest, opText } from '../../src/digest/index.js'

const TEMPLATES = ['cw', 'gcs']
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const optStr = (v: unknown): v is string | null | undefined => v === undefined || v === null || typeof v === 'string'

/** The request, or why it's refused. */
export function parseOpRequest(b: unknown) {
  if (!isObj(b)) return 'body: a JSON object'
  const { month, rows, cfg, params, plot_url, prev_url } = b
  if (typeof month !== 'string' || !MONTH_RE.test(month)) return 'month: YYYY-MM'
  if (!isObj(cfg) || !TEMPLATES.includes(cfg.template as string)) return `cfg.template: ${TEMPLATES.map(t => `'${t}'`).join(' | ')}`
  if (!isObj(rows) || rows.template !== cfg.template || !Array.isArray(rows.rows) || !rows.rows.length) {
    return `rows: the ${cfg.template} template's rows, at least one scan`
  }
  if (!optStr(params) || !optStr(plot_url) || !optStr(prev_url)) return 'params, plot_url, prev_url: strings'
  let p
  try {
    p = parseParams(params ?? '')
  } catch (e) {
    return `params: ${(e as Error).message}`
  }
  return { month, rows: rows as unknown as DigestRows, cfg: cfg as unknown as DigestCfg, params: p, links: { plotUrl: plot_url ?? null, prevUrl: prev_url ?? null } }
}

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const req = parseOpRequest(await ctx.request.json().catch(() => null))
  if (typeof req === 'string') return json({ error: req }, 400)
  const { month, rows, cfg, params, links } = req
  return json({ text: opText(rows, month, cfg, links, params), image: imageRequest(rows, month, cfg, params) }, 200, { 'cache-control': 'no-store' })
}
