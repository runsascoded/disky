import { useState } from 'react'
import { useOwnerMutations } from './owners'
import { Tooltip } from './Tooltip'
import { UserChip, allUsers } from './UserChip'

export const ASSIGN_TIP =
  'Assign this prefix an owner — you by default, or anyone you name. Overrides the inferred owner (paths, W&B runs, sidecars) and pulls it out of the “Unowned” pool so it counts as that person’s data.'

/**
 * Ownership assignment as one native select — "me", then everyone the site
 * knows — for the owner panel and every table row alike. Choosing a name
 * saves at once (the tooltip says so); the control resets to its placeholder
 * because the assignee then shows as the row's / panel's owner.
 */
export function AssignSelect({ prefix, assigned, compact, label }: {
  /** `gs://…/` prefix(es) to assign (trailing slash). Many = one batched POST. */
  prefix: string | string[]
  /** Current assignee id, if any (drives the placeholder and offers unassign). */
  assigned?: string | null
  /** Table-cell sizing. */
  compact?: boolean
  /** Placeholder override (the selection bar says "assign N…"). */
  label?: string
}) {
  const { post } = useOwnerMutations()
  const prefixes = Array.isArray(prefix) ? prefix : [prefix]
  const users = [...new Map(allUsers().map(u => [u.name, u])).values()]
  // A whole bucket is never a casual assignment: the newest action on an
  // ancestor wins, so it also overrides every assignment beneath it. Ask once.
  const buckets = prefixes.filter(isBucketPrefix)
  const [pending, setPending] = useState<string | null | undefined>(undefined)
  const save = (owner: string | null) => { post.mutate(prefixes.map(p => ({ pattern: p, owner }))); setPending(undefined) }
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
          if (buckets.length) setPending(owner)
          else save(owner)
        }}
      >
        <option value="">{label ?? (assigned ? 'reassign…' : 'assign…')}</option>
        <option value="@me">me</option>
        {users.map(u => <option key={u.id} value={u.id}>{u.name}</option>)}
        {(assigned || prefixes.length > 1) && <option value="@none">— unassign —</option>}
      </select>
    </Tooltip>
  )
}

/** `gs://bucket/` — a whole bucket, no path below it. */
export const isBucketPrefix = (p: string): boolean => /^[a-z0-9]+:\/\/[^/]+\/$/.test(p)

/** The assignee by name + when, for a row or the panel. */
export function Assignee({ who, ts, size = 15 }: { who: string; ts?: number; size?: number }) {
  return (
    <>
      <UserChip who={who} size={size} />
      {ts != null && <span className="prov">assigned {new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>}
    </>
  )
}
