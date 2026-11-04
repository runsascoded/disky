// `/runs/<id>` unfurl: a deletion run's state, by count only (a crawler is
// unauthenticated — no prefixes, sizes or names), and the site's image.
import { assetImage, unfurlShell } from '../_lib/unfurl.js'

interface Env {
  ASSETS: { fetch: (req: Request) => Promise<Response> }
  DB?: D1Database
}

/** What an unfurl may say of a run. */
export interface RunCard {
  mode: 'dry' | 'real'
  scan: string
  started_ts: number
  finished_ts: number | null
  deleted_objects: number
  buckets: string | null
}

const utc = (s: number): string => `${new Date(s * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`

export function runMeta(run: RunCard | null): { title: string; desc: string } {
  if (!run) return { title: 'Deletion run', desc: 'A deletion run. Sign in to see it.' }
  const n = run.buckets ? run.buckets.split(',').length : 0
  const where = n ? [`${n} ${n === 1 ? 'bucket' : 'buckets'}`] : []
  const dry = run.mode === 'dry'
  const name = dry ? 'Dry run' : 'Deletion'
  if (run.finished_ts == null) {
    return { title: `${name} running`, desc: [`Since ${utc(run.started_ts)}`, ...where, `scan ${run.scan}`, 'sign in for progress'].join(' · ') }
  }
  const objects = `${run.deleted_objects.toLocaleString('en-US')} ${run.deleted_objects === 1 ? 'object' : 'objects'} ${dry ? 'would be deleted' : 'attempted'}`
  return { title: `${name} finished`, desc: [`${objects}`, `finished ${utc(run.finished_ts)}`, ...where, `scan ${run.scan}`].join(' · ') }
}

export const onRequest = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const url = new URL(ctx.request.url)
  const id = decodeURIComponent(url.pathname.slice('/runs/'.length))
  let run: RunCard | null = null
  if (ctx.env.DB && id) {
    try {
      run = await ctx.env.DB.prepare('SELECT mode, scan, started_ts, finished_ts, deleted_objects, buckets FROM deletion_runs WHERE run_id = ?').bind(id).first<RunCard>()
    } catch {
      // A deployment without the deletion tables unfurls as a bare run.
    }
  }
  return unfurlShell(ctx, {
    ...runMeta(run),
    image: await assetImage(ctx, '/og-staged.jpg') ?? `${url.origin}/og.jpg`,
    page: url.href,
  })
}
