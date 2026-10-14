// One staging session, as /staged shows it: a stager who stages row by row
// (one POST, so one `stage_batches` row, per click) made one batch, not ten.
// The rows stay per request (each keeps its own time and permalink); the page
// groups them, by the rule the plan's Slack thread coalesces its replies with
// (`stagedSlack.ts` `COALESCE_S` is this window).

/** A stager's batches each within this many seconds of their previous one (same note) are one group. */
export const BATCH_GAP_S = 15 * 60

export interface BatchLike { id: number; created_by: string; created_ts: number; note: string | null }

/** `batches` grouped into staging sessions, each oldest first, the groups oldest first: per stager
 *  (case-insensitive) and note, a batch joins the group its previous batch is in when it starts within
 *  `gapS` of it. Another stager's batches in between don't split a group. */
export function coalesceBatches<B extends BatchLike>(batches: readonly B[], gapS: number = BATCH_GAP_S): B[][] {
  const groups: B[][] = []
  const open = new Map<string, B[]>()
  const order = [...batches].sort((a, b) => a.created_ts - b.created_ts || a.id - b.id)
  for (const b of order) {
    const k = `${b.created_by.toLowerCase()}\n${b.note ?? ''}`
    const g = open.get(k)
    if (g && b.created_ts - g[g.length - 1].created_ts <= gapS) { g.push(b); continue }
    const fresh = [b]
    groups.push(fresh)
    open.set(k, fresh)
  }
  return groups
}

/** Each batch id → its group's lead (its oldest batch's id). */
export function batchLeads(groups: readonly (readonly BatchLike[])[]): Map<number, number> {
  return new Map(groups.flatMap(g => g.map(b => [b.id, g[0].id] as [number, number])))
}

/** Batch ids as `#a–b` runs: `[30, 31, 32, 35]` → `#30–32, #35`. */
export function idRanges(ids: readonly number[]): string {
  const xs = [...ids].sort((a, b) => a - b)
  const runs: string[] = []
  for (let i = 0; i < xs.length;) {
    let j = i
    while (j + 1 < xs.length && xs[j + 1] === xs[j] + 1) j++
    runs.push(j > i ? `#${xs[i]}–${xs[j]}` : `#${xs[i]}`)
    i = j + 1
  }
  return runs.join(', ')
}
