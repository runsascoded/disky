import { useMemo } from 'react'
import { FaRegTrashCan } from 'react-icons/fa6'
import { useCanStage } from './auth'
import { useStagedPlan } from './plans'
import { stagedIndex, type StagedIndex, type StagedMark } from './stagedIndex'
import { useStore } from './store'
import { ActStatus, useActDeps, useKeepFocus, useMatchAct } from './MatchActions'
import { Tooltip } from './Tooltip'
import type { CoverItem } from './filterCover'
import type { TreeNode } from './types'

/** The unfiltered row is already an exact target; no match-list request is needed. */
export const tableCoverItem = (node: TreeNode, path: string): CoverItem => ({ path, kind: node.k ?? 'dir', b: node.b, o: node.o, roots: 1 })

export function PrefixStageAction({ item, scheme, fmtBytes, mark }: {
  item: CoverItem
  scheme: string
  fmtBytes: (b: number) => string
  /** Already staged (itself, or under a staged folder): a chip to `/staged` instead of the trash, once
   *  this row's own action (and its undo) is done. */
  mark?: StagedMark | null
}) {
  const send = useActDeps()
  const act = useMatchAct({ ...send, scheme, resolve: async () => ({ items: [item], complete: true }) }, `${scheme}${item.path}|${send.asOf?.()}`)
  const focus = useKeepFocus(act.state.s)
  return <span className="mact" tabIndex={-1} ref={focus.ref} {...focus.handlers}>
    {act.state.s === 'idle' && mark && mark.as !== 'inside' && <StagedChip mark={mark} />}
    {act.state.s === 'idle' && !(mark && mark.as !== 'inside') && <Tooltip placement="right" content={item.kind === 'file' ? 'Stage this file for deletion (exactly this object) — an admin approves and dispatches from /staged' : 'Stage this folder for deletion — an admin approves and dispatches from /staged'}>
      <button type="button" className="trash" onClick={() => void act.start({ kind: 'stage' })} aria-label="trash"><FaRegTrashCan /></button>
    </Tooltip>}
    <ActStatus state={act.state} confirmT={act.confirmT} act={act} fmtBytes={fmtBytes} />
  </span>
}

/** The open plan's index, for a viewer who can stage on a staging store (null otherwise, or while
 *  loading): what the map and table mark as already staged. */
export function useStagedIndex(): StagedIndex | null {
  const store = useStore()
  const canStage = useCanStage()
  const on = !!store.staging && canStage
  const q = useStagedPlan(false, null, on)
  return useMemo(() => (on && q.data ? stagedIndex(q.data.items) : null), [on, q.data])
}

/** A row's standing in the open plan, in place of its trash button: staged itself, or under a staged
 *  folder — linking to `/staged`, where it's reviewed, unstaged or dispatched. */
export function StagedChip({ mark }: { mark: StagedMark & { as: 'self' | 'covered' } }) {
  return <Tooltip placement="right" content={mark.as === 'self' ? 'Staged for deletion: an admin reviews and dispatches it from /staged' : <>Inside staged folder <code>{mark.by}</code></>}>
    <a className="staged-chip" href="/staged">{mark.as === 'self' ? 'staged' : 'in staged'}</a>
  </Tooltip>
}
