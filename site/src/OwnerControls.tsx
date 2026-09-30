import { DEFAULT_STORE } from './stores'
import { AssignSelect } from './AssignSelect'
import { OwnerFactChip, fmtDate } from './OwnerFactChip'
import { useCanAssign } from './auth'
import type { OwnerIndex } from './owners'
import type { TreeNode } from './types'
import type { UserIndexEntry } from './colors'
import { ClassBar, OwnerBar, ownerShares } from './OwnerBar'

// The ownership panel for one prefix — rendered inside the pinned treemap
// tooltip and above the map for the current drill root. Shows the node's
// effective owner (an assignment from the ledger, most-recent-wins over
// ancestor assignments, else the scan's attribution) with provenance, and the
// assign control for admins.

/**
 * `node`: the tree node behind `uri`, when the caller has it — feeds the
 * ownership bar (absent for typed prefixes below the depth cap). `lensed`: the
 * view is already filtered to one owner, so `node`'s user split is that person
 * alone. `userIdx`: the site's user palette, so the ownership bar's colors
 * match the color-by-owner map.
 */
export function OwnerControls({ uri, idx, node, lensed, userIdx, onPickUser }: {
  uri: string; idx: OwnerIndex; node?: TreeNode; lensed?: boolean; userIdx?: Map<string, UserIndexEntry>; onPickUser?: (u: string) => void
}) {
  const canAssign = useCanAssign()
  if (!DEFAULT_STORE.owners || !uri.startsWith('gs://') || uri.indexOf('/', 5) === -1) {
    // store root ("gs://…" with no bucket path) — nothing assignable
    return null
  }
  const prefix = uri.endsWith('/') ? uri : uri + '/'
  const cl = idx.claimOf(uri)
  // Ownership: the assignment if there is one; otherwise the scan's
  // attribution — one person by name, a mix as a bar (OwnerBar).
  const shares = ownerShares(node ?? { n: '', b: 0, o: 0 } as TreeNode)
  const soleOwner = !cl && shares.length > 0 && shares[0][1] >= 0.98 * node!.b ? shares[0][0] : null
  const mixed = !cl && !soleOwner && shares.length > 0
  return (
    <div className="owner-controls" onClick={e => e.stopPropagation()}>
      <span className="owner">
        <span className="lbl">{mixed ? 'owners' : 'owner'}</span>
        {cl
          ? <><OwnerFactChip who={cl.who} assigned={{ by: cl.by, ts: cl.ts, memo: cl.memo }} /><span className="prov">assigned {fmtDate(cl.ts)}</span></>
          : soleOwner
            ? <OwnerFactChip who={soleOwner} inferred={node?.pv ?? null} />
            : mixed
              ? <OwnerBar node={node!} userIdx={userIdx} onPickUser={onPickUser} note="Nobody has assigned this prefix. The scan attributes its bytes to:" />
              // The lens keeps only one person's bytes: with none here, the
              // attributed owners are people the lens hides — not "none".
              : <span className="none" title={lensed ? 'the owner filter hides other people\'s bytes here' : undefined}>—</span>}
        {canAssign && <AssignSelect prefix={prefix} assigned={cl?.who ?? null} />}
        {node && node.cb && Object.values(node.cb).some(b => b > 0) && (
          <span className="classes">
            <span className="lbl">class</span>
            <ClassBar node={node} />
          </span>
        )}
      </span>
    </div>
  )
}
