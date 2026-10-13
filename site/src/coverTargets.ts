// What an action on a filter's matches takes from its cover (DOM-free: the Functions' tests drive the action
// through these too): a row's items, and the items an action sends, by kind.
import type { CoverItem } from '../functions/_lib/cover'
import type { PlanItem } from './batches'

/** What an action resolves to: the items it may take, and whether that is every match (`complete` false:
 *  `reason` says why, shown muted in place of the control). */
export interface Resolved { items: CoverItem[]; complete: boolean; reason?: string }

const under = (p: string, a: string) => p === a || p.startsWith(a + '/')
const depthOf = (p: string) => (p === '' ? 0 : p.split('/').length)

/** The items a table row (path below the root) holds: those at or under it. */
export const rowItems = (items: readonly CoverItem[], row: string): CoverItem[] => items.filter(i => under(i.path, row))

/** What an action sends for the kept items, and what it leaves out and why. A folder every object of which
 *  matches goes as a prefix (`…/`); a lone matching file goes as an exact object item (specs/file-assign.md:
 *  it owns / deletes that one key, never `key.bak`). Staging never takes a whole bucket (a plan item can't
 *  name one). An item whose kind the server couldn't look up is never sent: file or folder decides what it
 *  names. `folders` / `files`: how many of each `items` holds. */
export interface Targets { items: PlanItem[]; b: number; o: number; folders: number; files: number; buckets: number; unknown: number }
export function actionTargets(items: readonly CoverItem[], scheme: string, action: 'assign' | 'stage'): Targets {
  const t: Targets = { items: [], b: 0, o: 0, folders: 0, files: 0, buckets: 0, unknown: 0 }
  for (const i of items) {
    if (i.kind === null) { t.unknown++; continue }
    if (i.kind === 'dir' && action === 'stage' && depthOf(i.path) < 2) { t.buckets++; continue }
    if (i.kind === 'file') {
      t.items.push({ key: `${scheme}${i.path}`, kind: 'object' })
      t.files++
    } else {
      t.items.push({ key: `${scheme}${i.path}/`, kind: 'prefix' })
      t.folders++
    }
    t.b += i.b
    t.o += i.o
  }
  return t
}

/** What an action sends, in words: `2 folders, 3 files`. */
export function targetsText(t: { folders: number; files: number }): string {
  const n = (k: number, one: string) => `${k.toLocaleString('en-US')} ${k === 1 ? one : `${one}s`}`
  const parts = [t.folders && n(t.folders, 'folder'), t.files && n(t.files, 'file')].filter(Boolean)
  return parts.length ? parts.join(', ') : 'nothing'
}
