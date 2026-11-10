import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { HistoryError, historyRequestBody, requiredHistoryPins } from '../../_lib/historyAdmin.js'
import { adoptHistoryVersion, historyPointer, HistoryVersionError, loadHistoryVersion, type HistoryVersionEnv } from '../../_lib/historyVersion.js'

const NO_STORE = { 'cache-control': 'no-store' }
type HistoryCtx = Omit<Ctx, 'env'> & { env: Ctx['env'] & HistoryVersionEnv }

export function parseHistoryAdoption(value: unknown): { version: string; expectedEtag: string | null } {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new HistoryError('Expected an object')
  const body = value as Record<string, unknown>
  if (typeof body.version !== 'string' || !body.version || body.version.length > 160) throw new HistoryError('version must name an immutable history version')
  if (body.expected_etag !== null && (typeof body.expected_etag !== 'string' || !body.expected_etag || body.expected_etag === '*')) throw new HistoryError('expected_etag must be the exact current ETag, or null for an absent pointer')
  return { version: body.version, expectedEtag: body.expected_etag }
}

export const onRequestGet = async (ctx: HistoryCtx): Promise<Response> => {
  const actor = await requireAdmin(ctx)
  if (actor instanceof Response) return actor
  if (!ctx.env.HISTORY_CURRENT_KEY || !ctx.env.INDEX_R2) return json({ enabled: false, reason: 'This environment has no history version pointer configured', pointer: null }, 200, NO_STORE)
  try {
    const pointer = await historyPointer(ctx.env)
    const version = pointer ? await loadHistoryVersion(ctx.env.INDEX_R2, pointer.version) : null
    return json({ enabled: true, pointer, version }, 200, NO_STORE)
  } catch (e) {
    if (e instanceof HistoryVersionError) return json({ error: e.message, code: e.code }, e.status, NO_STORE)
    throw e
  }
}

export const onRequestPost = async (ctx: HistoryCtx): Promise<Response> => {
  const actor = await requireAdmin(ctx)
  if (actor instanceof Response) return actor
  if (!ctx.env.DB) return json({ error: 'History adoption requires DB to verify review and recovery pins' }, 503, NO_STORE)
  try {
    const input = parseHistoryAdoption(await historyRequestBody(ctx.request))
    const requiredPins = await requiredHistoryPins(ctx.env.DB, Date.now())
    return json(await adoptHistoryVersion(ctx.env, { ...input, requiredPins }), 200, NO_STORE)
  } catch (e) {
    if (e instanceof HistoryVersionError) return json({ error: e.message, code: e.code }, e.status, NO_STORE)
    if (e instanceof HistoryError) return json({ error: e.message }, e.status, NO_STORE)
    throw e
  }
}
