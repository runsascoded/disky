import { useState } from 'react'
import { ownerPost, useOwnerMutations } from './owners'
import type { PlanItem } from './batches'
import { assignInBatches } from './filterCover'
import { isBucketPrefix } from './batches'

export { isBucketPrefix }
import { Tooltip } from './Tooltip'
import { UserChip, allUsers } from './UserChip'

export const ASSIGN_TIP =
  'Assign this folder (or file) an owner — you by default, or anyone you name. Overrides the inferred owner (paths, W&B runs, sidecars) and pulls it out of the “Unowned” pool so it counts as that person’s data.'

/**
 * Ownership assignment as one native select — "me", then everyone the site
 * knows — for the owner panel and every table row alike. Choosing a name
 * saves at once (the tooltip says so); the control resets to its placeholder
 * because the assignee then shows as the row's / panel's owner.
 */
export function AssignSelect({ items: item = [], assigned, compact, label, onPick }: {
  /** What to assign: folder prefixes (`gs://…/`) and exact objects, each with its kind. Many = one batched POST. */
  items?: PlanItem | PlanItem[]
  /** Current assignee id, if any (drives the placeholder and offers unassign). */
  assigned?: string | null
  /** Table-cell sizing. */
  compact?: boolean
  /** Placeholder override (the selection bar says "assign N…"). */
  label?: string
  /** The pick goes here instead of being saved (`owner` null: unassign; `who` the name picked): a filtered
   *  row's matches are only listed once picked (`useMatchAct`), which then confirms and sends them. */
  onPick?: (owner: string | null, who: string) => void
}) {
  const { post } = useOwnerMutations()
  const items = Array.isArray(item) ? item : [item]
  const users = [...new Map(allUsers().map(u => [u.name, u])).values()]
  // A whole bucket is never a casual assignment: the newest action on an
  // ancestor wins, so it also overrides every assignment beneath it. Ask once.
  const buckets = items.filter(i => i.kind === 'prefix' && isBucketPrefix(i.key)).map(i => i.key)
  const [pending, setPending] = useState<string | null | undefined>(undefined)
  // `ASSIGN_CHUNK` per POST (the API takes ≤ 500 actions at once): a filtered row's or selection's matches may be more.
  const save = (owner: string | null) => { void assignInBatches(items, i => ownerPost(i, owner), a => post.mutateAsync(a)).catch(() => {}); setPending(undefined) }
  if (pending !== undefined) {
    const name = pending === null ? 'nobody' : pending === '@me' ? 'you' : (users.find(u => u.id === pending)?.name ?? pending)
    return (
      <span className="assign-confirm">
        {buckets.length === 1 ? <>whole bucket <code>{buckets[0]}</code></> : <>{buckets.length} whole buckets</>} → {name}? This overrides every assignment under it.
        <button type="button" className="danger" onClick={() => save(pending)}>assign</button>
        <button type="button" className="quiet" onClick={() => setPending(undefined)}>cancel</button>
      </span>
    )
  }
  return (
    <Tooltip content={<>{ASSIGN_TIP}<div className="how">Choosing a name saves immediately.</div></>}>
      <select
        className={`assign-sel${compact ? ' compact' : ''}`} value="" aria-label="assign owner"
        onChange={e => {
          const v = e.target.value
          if (!v) return
          const owner = v === '@none' ? null : v
          if (onPick) onPick(owner, owner === null ? 'nobody' : owner === '@me' ? 'you' : (users.find(u => u.id === owner)?.name ?? owner))
          else if (buckets.length) setPending(owner)
          else save(owner)
        }}
      >
        <option value="">{label ?? (assigned ? 'reassign…' : 'assign…')}</option>
        <option value="@me">me</option>
        {users.map(u => <option key={u.id} value={u.id}>{u.name}</option>)}
        {(assigned || items.length > 1 || onPick) && <option value="@none">— unassign —</option>}
      </select>
    </Tooltip>
  )
}


/** The assignee by name + when, for a row or the panel. */
export function Assignee({ who, ts, size = 15 }: { who: string; ts?: number; size?: number }) {
  return (
    <>
      <UserChip who={who} size={size} />
      {ts != null && <span className="prov">assigned {new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>}
    </>
  )
}
