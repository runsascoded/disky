// `/runs/<id>` unfurl. The shell is public (an SPA has to be), but its stamp is the run's only to a signed-in viewer
// (state and progress, by count only, and its drawn card, `_lib/og/run.ts`). Anyone else, a crawler included, gets one
// fixed stamp whether the id exists or not, and no run is read. `/runs` isn't an `OgKind`, so the middleware leaves
// this stamp alone.
import { requireViewer, type Env } from '../_lib/auth.js'
import { doneVerb, runIdOfPath, runImagePath, runTitle, utcMinute, type RunRow, type RunShape } from '../_lib/og/run.js'
import { runAt, runShapeAt } from '../_lib/og/runData.js'
import { assetImage, unfurlShell } from '../_lib/unfurl.js'

type PageEnv = Env & {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  OG_CARDS?: string
}

/** What an anonymous caller is told of any id. */
export const GENERIC_RUN_META = { title: 'Deletion run', desc: 'A deletion run. Sign in to see it.' }

/** The description: the card's words, plus the scan. */
export function runMeta(run: Pick<RunRow, 'scan'>, s: RunShape): { title: string; desc: string } {
  const n = s.n_buckets
  const fmt = (v: number) => v.toLocaleString('en-US')
  const objects = s.done == null ? (s.total != null ? `${fmt(s.total)} objects planned` : 'No progress yet')
    : `${fmt(s.done)}${s.total != null ? ` of ${fmt(s.total)}` : ''} ${(s.total ?? s.done) === 1 ? 'object' : 'objects'} ${s.mode === 'dry' ? 'would be deleted' : doneVerb(s.mode)}`
  return {
    title: runTitle(s),
    desc: [
      objects,
      s.as_of != null ? `as of ${utcMinute(s.as_of)}` : `since ${utcMinute(s.started_ts)}`,
      ...(n ? [`${n} ${n === 1 ? 'bucket' : 'buckets'}`] : []),
      ...(s.unanswered ? [`${fmt(s.unanswered)} unanswered`] : []),
      `scan ${run.scan}`,
    ].join(' · '),
  }
}

export const onRequest = async (ctx: { request: Request; env: PageEnv }): Promise<Response> => {
  const url = new URL(ctx.request.url)
  const viewer = !(await requireViewer(ctx) instanceof Response)
  const id = viewer ? runIdOfPath(url.pathname) : null
  const t = Math.floor(Date.now() / 1000)
  const at = id ? await runAt(ctx.env, id, t) : null
  const shape = at ? (await runShapeAt(ctx.env, url.origin, at, t)).shape : null
  const res = await unfurlShell(ctx, {
    ...(at && shape ? runMeta(at.run, shape) : GENERIC_RUN_META),
    image: at && ctx.env.OG_CARDS ? url.origin + runImagePath(at) : await assetImage(ctx, '/og-staged.jpg') ?? `${url.origin}/og.jpg`,
    ...(at && ctx.env.OG_CARDS ? { imageType: 'image/png' } : {}),
    page: url.href,
  })
  // The stamp changes with the viewer and the run: never revalidated against the shell's own etag.
  const headers = new Headers(res.headers)
  headers.set('cache-control', 'private, no-store')
  return new Response(res.body, { status: res.status, headers })
}
