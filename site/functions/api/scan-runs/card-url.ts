// GET /api/scan-runs/card-url[?store=<key>][&theme=light][&color=<rrggbb>] — a freshly signed URL for the deployment's
// status card (`_lib/og/deployData.ts`, open question #87), for the scan job's run thread head (`dt_cloud.nominal`):
//
//   → { url, v, expires }      viewer (the job's grant), as `/api/scan-runs`
//     503: no `SESSION_SECRET` (nothing to sign with) or no D1
//
// `url` is `/og/deploy.png`, signed for `IMAGE_TTL_DAYS` (`expires`: epoch s, the end of its last day) and good without
// a credential (Slack fetches it with none). `v` names the runs and sizes it was minted over: the same within a day until
// a run or a scan changes them. `color` is the card's dot (`#` optional; anything else is ignored: the grey default).
import { json, requireViewer } from '../../_lib/auth.js'
import { withPathStore } from '../../_lib/index.js'
import { colorParam, deployCardUrl, type DeployEnv } from '../../_lib/og/deployData.js'
import { deploymentKey, type OgEnv } from '../../_lib/og/serve.js'
import { DAY_S, EPOCH } from '../../_lib/og/sign.js'
import { withStore } from '../../_lib/stores.js'

const NO_STORE = { 'cache-control': 'no-store' }

export const onRequestGet = async (ctx0: { request: Request; env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const keyP = deploymentKey(ctx.env)
  if (!keyP) return json({ error: 'card URLs not configured (SESSION_SECRET unset)' }, 503, NO_STORE)
  if (!ctx.env.DB) return json({ error: 'no D1 bound' }, 503, NO_STORE)
  const url = new URL(ctx.request.url)
  const { env } = await withPathStore(ctx)
  const now = Math.floor(Date.now() / 1000)
  const opts = { ...(url.searchParams.get('theme') === 'light' ? { theme: 'light' as const } : {}), c: colorParam(url.searchParams.get('color')) }
  const { url: card, v, day } = await deployCardUrl(await keyP, env as DeployEnv, url.origin, now, opts)
  return json({ url: card, v, expires: EPOCH + (day + 1) * DAY_S }, 200, NO_STORE)
}
