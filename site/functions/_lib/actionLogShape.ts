/** `GET /api/actions?log=1` row shapes. Import-free: the client
 * (`src/ActionLog.tsx`) imports them too. */

/** What became of an action: still in force; replaced by a newer action on
 * the same prefix; overridden by a newer action on an ancestor (most recent
 * wins across nesting); or retracted (its `owner_prefixes` row tombstoned). */
export type ActionStatus = 'live' | 'superseded' | 'overridden' | 'retracted'

export interface ActionLogRow {
  id: number
  ts: number
  /** The actor's email. */
  who: string
  prefix: string
  /** The owner it set; null = cleared. */
  owner: string | null
  memo: string | null
  status: ActionStatus
  /** The retraction note, when retracted. */
  retracted: string | null
}
