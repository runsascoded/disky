// Bulk actions over the filter's matches (`gcs:specs/done/selection-actions.md`): assign an owner to, or
// stage for deletion, every match under the view — as the fewest exact items (`/api/filter-cover`: a
// folder every object of which matches stands for its matches; a lone matching file is sent as that one
// object, specs/file-assign.md), listed by parent folder so false positives can be dropped (a whole folder's
// worth, or one by one) before anything is sent. Offered at once: hovering or focusing the bar starts the
// listing, a click awaits it (`useMatchAct`), then asks (with the review list), sends in batches with a
// progress bar, and says what landed — or, over the cap, says so muted in the button's place.
import { useEffect, useMemo, useState } from 'react'
import { allUsers } from './UserChip'
import { useUnits } from './units'
import { type CoverItem, type FilterCover, groupItems } from './filterCover'
import { ActStatus, useActDeps, useKeepFocus, useMatchAct } from './MatchActions'
import { actBlockedLabel } from './matchAct'
import { Tooltip } from './Tooltip'

export { caveats } from './matchAct'

/** Items listed per folder in the review panel (the folder's checkbox still takes or drops all of them). */
export const LIST_PER_GROUP = 100

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

/** The item paths left out by the viewer: per item, or per folder (every item in it). */
export function useDropped(cover: FilterCover | undefined) {
  const [dropped, setDropped] = useState<ReadonlySet<string>>(new Set())
  useEffect(() => setDropped(new Set()), [cover])
  const toggle = (paths: string[], drop: boolean) => setDropped(d => applyToggle(d, paths, drop))
  return { dropped, toggle }
}

/** `dropped` with `paths` dropped (`drop`) or taken back. */
export function applyToggle(dropped: ReadonlySet<string>, paths: readonly string[], drop: boolean): ReadonlySet<string> {
  const n = new Set(dropped)
  for (const p of paths) if (drop) n.add(p); else n.delete(p)
  return n
}

/** The kept items in words: `2 folders, 3 files` (unchecked ones counted as `matches`). */
export function itemsText(items: readonly CoverItem[]): string {
  const dirs = items.filter(i => i.kind === 'dir').length, files = items.filter(i => i.kind === 'file').length
  const unk = items.length - dirs - files
  const parts = [dirs && plural(dirs, 'folder'), files && plural(files, 'file'), unk && `${plural(unk, 'unchecked match', 'unchecked matches')}`].filter(Boolean)
  return parts.length ? parts.join(', ') : 'nothing'
}

export function BulkBar({ cover, loading, onIntent, resolve, resetKey, scheme, query, canAssign, canStage, blocked }: {
  /** The view's cover once listed (prefetched on intent, or by a click): the summary and review list. */
  cover?: FilterCover
  /** The listing is in flight (a hover started it). */
  loading?: boolean
  /** Hover / focus on the bar: start listing the matches (deduplicated, cached). */
  onIntent?: () => void
  /** The view's cover, awaited by a click (the same fetch the hover started). */
  resolve: () => Promise<FilterCover>
  /** The scan, path and filter: a change starts the bar over. */
  resetKey?: string
  scheme: string
  query: string
  canAssign: boolean
  canStage: boolean
  /** Why this view's matches can't be acted on (`actBlock`): the actions give way to a muted label whose
   *  tooltip says why, and nothing is listed. */
  blocked?: string | null
}) {
  const send = useActDeps()
  const { fmtBytes } = useUnits()
  const { dropped, toggle } = useDropped(cover)
  const [assign, setAssign] = useState('')
  const [memo, setMemo] = useState('')
  const act = useMatchAct({ ...send, scheme, resolve, alwaysConfirm: true, pick: got => got.items.filter(i => !dropped.has(i.path)) }, resetKey)
  const focus = useKeepFocus(act.state.s)
  const items = useMemo(() => cover?.complete ? cover.items : [], [cover])
  const kept = useMemo(() => items.filter(i => !dropped.has(i.path)), [items, dropped])
  const groups = useMemo(() => groupItems(items), [items])

  if (!canAssign && !canStage) return null
  if (blocked) return (
    <span className="bulkbar blocked">
      <Tooltip content={<span className="bb-tip">{blocked}</span>}><span className="act-st muted" tabIndex={0}>{actBlockedLabel(blocked)}</span></Tooltip>
    </span>
  )
  const idle = act.state.s === 'idle'
  const keptB = kept.reduce((n, i) => n + i.b, 0)
  const startAssign = () => {
    const v = assign.trim()
    const owner = v ? (allUsers().find(u => u.name.toLowerCase() === v.toLowerCase())?.id ?? v) : '@me'
    void act.start({ kind: 'assign', owner, who: v || 'you', memo: `bulk filter:'${query}'${memo ? ` — ${memo}` : ''}` })
  }
  const startStage = () => void act.start({ kind: 'stage', memo })
  const summary = !cover ? (loading ? <span className="bb-scope">listing the matches…</span> : null)
    : !cover.roots.n ? <span className="bb-scope">no matches to act on</span>
    : !cover.complete ? <span className="bb-scope">{plural(cover.roots.n, 'match', 'matches')} · {fmtBytes(cover.roots.b)}</span>
    : <>
      <span className="bb-scope">
        {plural(cover.roots.n, 'match', 'matches')} → {itemsText(kept)}{kept.length < items.length ? ` (${(items.length - kept.length).toLocaleString('en-US')} unticked)` : ''} · {fmtBytes(keptB)}
      </span>
      <details className="bb-review">
        <summary>review</summary>
        <div className="bb-panel" role="group" aria-label="Matches to act on">
          <p className="bb-help">Each line is a folder whose every file matches (sent as that folder), or a single matching file (sent as exactly that file — never its neighbours). Untick false positives — a whole folder’s worth, or one at a time.</p>
          <div className="bb-bulk">
            <button type="button" className="act" onClick={() => toggle(items.map(i => i.path), false)}>all</button>
            <button type="button" className="act" onClick={() => toggle(items.map(i => i.path), true)}>none</button>
          </div>
          <ul className="bb-groups">
            {groups.map(g => <ReviewGroup key={g.parent} parent={g.parent} items={g.items} b={g.b} dropped={dropped} toggle={toggle} fmtBytes={fmtBytes} />)}
          </ul>
        </div>
      </details>
    </>

  return (
    <span className="bulkbar" onMouseEnter={onIntent} onFocus={onIntent} aria-busy={loading || undefined}>
      <span className="mact bb-acts" tabIndex={-1} ref={focus.ref} {...focus.handlers}>
        {idle && <>
          {canAssign && <>
            <button type="button" className="act assign" onClick={startAssign}>{assign.trim() ? 'assign' : 'assign to me'}</button>
            <input list="bb-assign-users" value={assign} onChange={e => setAssign(e.target.value)} placeholder="you" size={7} aria-label="Assign matches to user" />
            <datalist id="bb-assign-users">{allUsers().map(u => <option key={u.id} value={u.name} />)}</datalist>
          </>}
          {canStage && <button type="button" className="act stage" onClick={startStage}>stage for deletion</button>}
        </>}
        <ActStatus state={act.state} confirmT={act.confirmT} act={act} fmtBytes={fmtBytes} />
      </span>
      <input value={memo} onChange={e => setMemo(e.target.value)} placeholder="memo" size={10} aria-label="Bulk memo" disabled={!idle} />
      {summary}
    </span>
  )
}

function ReviewGroup({ parent, items, b, dropped, toggle, fmtBytes }: {
  parent: string
  items: CoverItem[]
  b: number
  dropped: ReadonlySet<string>
  toggle: (paths: string[], drop: boolean) => void
  fmtBytes: (b: number) => string
}) {
  const keptN = items.filter(i => !dropped.has(i.path)).length
  const all = keptN === items.length
  const paths = items.map(i => i.path)
  const shown = items.slice(0, LIST_PER_GROUP)
  return (
    <li className="bb-group">
      <label className="bb-row bb-parent">
        <input type="checkbox" checked={all} ref={el => { if (el) el.indeterminate = keptN > 0 && !all }} onChange={() => toggle(paths, all)} aria-label={`all matches in ${parent || 'the root'}`} />
        <code>{parent ? `${parent}/` : '(root)'}</code>
        <span className="bb-n">{items.length > 1 ? `${keptN.toLocaleString('en-US')}/${items.length.toLocaleString('en-US')} · ` : ''}{fmtBytes(b)}</span>
      </label>
      <ul>
        {shown.map(i => (
          <li key={i.path}>
            <label className="bb-row">
              <input type="checkbox" checked={!dropped.has(i.path)} onChange={() => toggle([i.path], !dropped.has(i.path))} />
              <code>{i.path.slice(parent ? parent.length + 1 : 0)}{i.kind === 'dir' ? '/' : ''}</code>
              <span className="bb-n">{i.kind === 'dir' ? `${plural(i.o, 'file')} · ` : i.kind === null ? 'unchecked · ' : ''}{fmtBytes(i.b)}</span>
            </label>
          </li>
        ))}
        {items.length > shown.length && <li className="bb-more">…and {(items.length - shown.length).toLocaleString('en-US')} more (the folder’s box takes or drops them all)</li>}
      </ul>
    </li>
  )
}
