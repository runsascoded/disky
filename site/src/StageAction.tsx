import { FaRegTrashCan } from 'react-icons/fa6'
import { ActStatus, useActDeps, useKeepFocus, useMatchAct } from './MatchActions'
import { Tooltip } from './Tooltip'
import type { CoverItem } from './filterCover'
import type { TreeNode } from './types'

/** The unfiltered row is already an exact target; no match-list request is needed. */
export const tableCoverItem = (node: TreeNode, path: string): CoverItem => ({ path, kind: node.k ?? 'dir', b: node.b, o: node.o, roots: 1 })

export function PrefixStageAction({ item, scheme, fmtBytes }: { item: CoverItem; scheme: string; fmtBytes: (b: number) => string }) {
  const send = useActDeps()
  const act = useMatchAct({ ...send, scheme, resolve: async () => ({ items: [item], complete: true }) }, `${scheme}${item.path}|${send.asOf?.()}`)
  const focus = useKeepFocus(act.state.s)
  return <span className="mact" tabIndex={-1} ref={focus.ref} {...focus.handlers}>
    {act.state.s === 'idle' && <Tooltip placement="right" content={item.kind === 'file' ? 'Stage this file for deletion (exactly this object) — an admin approves and dispatches from /staged' : 'Stage this folder for deletion — an admin approves and dispatches from /staged'}>
      <button type="button" className="trash" onClick={() => void act.start({ kind: 'stage' })} aria-label="trash"><FaRegTrashCan /></button>
    </Tooltip>}
    <ActStatus state={act.state} confirmT={act.confirmT} act={act} fmtBytes={fmtBytes} />
  </span>
}
