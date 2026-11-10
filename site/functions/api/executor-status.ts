import { type Ctx, type Env as AuthEnv, json, requireAdmin } from '../_lib/auth.js'
import { executorOf, type ExecEnv } from '../_lib/executor.js'
import { laptopStatus } from '../_lib/laptopDispatch.js'

export const onRequestGet = async (ctx: Ctx & { env: AuthEnv & ExecEnv }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  if (executorOf(ctx.env) !== 'laptop') return json({ error: 'This executor has no laptop cleanup agent.' }, 404)
  if (!ctx.env.DB) return json({ error: 'Cleanup status unavailable: plans store not configured.' }, 503)
  return json(await laptopStatus(ctx.env.DB), 200, { 'cache-control': 'no-store' })
}
