/** What the buckets' soft delete is now, as far as the site knows (`_lib/softDeleteNow.ts`): each bucket on or off as of the
 * job's newest reading (the scan step `soft-delete`), so it is as old as the last scan that ran the step.
 *
 *   GET /api/sweep/soft-delete → {soft_delete: {asOf: "<date> <hh:mm>Z", off: {<bucket>: boolean}} | null}         viewer
 *
 * Any viewer: a run's row says what its undo depends on for everyone who can see it. Null: no reading (the step isn't run
 * here, or its document couldn't be read), or an executor whose undo isn't a soft-delete restore (`plan-sweep`). A bucket
 * the reading doesn't name is unknown. */
import { type Ctx, json, requireViewer } from '../../_lib/auth.js'
import { bucketSoftDelete } from '../../_lib/softDeleteNow.js'

type C = Ctx & { env: { EXECUTOR?: string } }

export const onRequestGet = async (ctx: C): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  return json({ soft_delete: ctx.env.EXECUTOR === 'sweep' ? await bucketSoftDelete(ctx.env) : null }, 200, { 'cache-control': 'private, max-age=300' })
}
