import { diffRemainderParent } from './diffRows'
import type { DiffNode } from './diffModel'

export const showDiffFolderListing = (): void => document.getElementById('tbl')?.scrollIntoView({ behavior: 'smooth', block: 'start' })

/** Synthetic cells have totals but no literal path of their own. Their pinned inspector offers the real folder,
 *  or the current folder's listing when that is already the comparison scope. The scan range stays unchanged. */
export function DiffRemainder({ node, onDrill }: {
  node: Pick<DiffNode, 'key' | 'label' | 'status'>
  onDrill?: (segs: string[]) => void
}) {
  const parent = diffRemainderParent(node)
  if (!parent) return null
  const nested = parent.length > 0
  return <div className="diff-remainder">
    <p>{node.status === 'filler'
      ? 'Unchanged entries omitted from this view.'
      : 'Grouped entries omitted at this zoom level; this is not a folder named “(other)”.'}</p>
    {nested && onDrill
      ? <button type="button" onClick={() => onDrill(parent)}>Open containing folder</button>
      : !nested && <button type="button" onClick={showDiffFolderListing}>View folder listing</button>}
    <span>{nested ? 'Keeps the selected scan range.' : 'The listing shows the after-scan contents, including entries outside this group.'}</span>
  </div>
}
