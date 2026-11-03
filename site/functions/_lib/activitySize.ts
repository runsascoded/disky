/**
 * An assignment's size for the activity feed (specs/done/activity-feed.md): looked up at the latest scan, as the staged
 * threads size a plan. Apart from `activityHook.ts` so the routes that note activity don't import the index reader.
 */
import type { Ctx } from './auth.js'
import { fmtBytes, type ServerEvent } from './activityShape.js'
import { stagedStats } from './stagedSlack.js'
import { withPathStore } from './index.js'

/** Assignments' sizes at the latest scan (`detail.bytes`, and on the line), best-effort: unsized when the index
 *  can't be read here. */
export async function sizeAssignments(ctx: Ctx, events: ServerEvent[]): Promise<void> {
  const assigned = events.filter(e => e.kind === 'assign' && typeof e.detail?.pattern === 'string')
  if (!assigned.length) return
  const at = await stagedStats((await withPathStore(ctx)).env, assigned.map(e => e.detail!.pattern as string)).catch(() => null)
  if (!at) return
  for (const e of assigned) {
    const b = at.stats[e.detail!.pattern as string]?.b
    if (typeof b !== 'number') continue
    e.detail!.bytes = b
    e.line = e.line.replace(/^(`[^`]*`)/, `$1 (${fmtBytes(b)})`)
  }
}
