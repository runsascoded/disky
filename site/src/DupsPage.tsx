// /dups (specs/dups.md §3): the scan's duplicate-candidate groups (`/api/dups`, from the `dups` scan step's report) for
// review and staging. The header totals each tier (net: copies under another group's copy count once) and shows which
// buckets duplicate which; each group expands to its copies, the suggested keep marked and the rest pre-checked; an
// admin stages the checked copies into the open plan (`useStage`) and finishes on /staged. Nothing deletes from here.
import { Fragment, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { boolParam, enumParam, stringParam, useUrlState } from 'use-prms'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { ClassMixTip, Tooltip } from './Tooltip'
import { useDocTitle } from './title'
import { useUnits } from './units'
import { useIsAdmin } from './auth'
import { useStage } from './plans'
import { setCurrentScan } from './owners'
import { DEFAULT_STORE } from './stores'
import { getJson } from './scanRunsUi'
import { CLASS_COLORS, CLASS_NAMES, classMix, fmtN } from './types'
import {
  type DupCopy, type DupGroup, type DupsResponse, type DupsSort, groupIdle, idleDays, pairMatrix, SORTS, sortGroups, stageBlock,
  stageItems, suggested, type Tier, today,
} from './dupsModel'
import './scans.scss'
import './dups.scss'

const TIER_TIPS: Record<Tier, string> = {
  tree: 'The same relative path (the path minus its bucket), with identical size and object count, in two or more buckets: a region copy. High confidence.',
  moved: 'The same basename, size and object count under different paths (one bucket can hold a dataset under several names). Needs a spot check: same-size collisions are common, so a moved copy is stageable only once its objects matched the keep copy\'s.',
}
const MINS: [string, number][] = [['any', 0], ['≥ 10 GB', 1e10], ['≥ 100 GB', 1e11], ['≥ 1 TB', 1e12], ['≥ 10 TB', 1e13]]
const CHECK_TIPS: Record<string, string> = {
  equal: "Every object's name, size and crc32c matched the keep copy's (complete listings).",
  partial: 'What both (capped) listings reached matched; the rest was not compared.',
  differs: "Some object or name differs from the keep copy's: not a duplicate.",
}

/** `/api/dups`'s query for the page's filters (the report's server-side ones; the sort is the page's). */
function useDups(p: { d?: string; tier: string; min: number; buckets?: string; q?: string; suspect: boolean; nested: boolean }) {
  const sp = new URLSearchParams()
  if (p.d) sp.set('d', p.d)
  if (p.tier) sp.set('tier', p.tier)
  if (p.min) sp.set('min', String(p.min))
  if (p.buckets) sp.set('buckets', p.buckets)
  if (p.q) sp.set('q', p.q)
  if (p.suspect) sp.set('suspect', '1')
  if (p.nested) sp.set('nested', '1')
  sp.set('n', '200')
  return useQuery<DupsResponse, Error>({ queryKey: ['dups', sp.toString()], queryFn: () => getJson(`/api/dups?${sp}`), staleTime: 60_000 })
}

const fmtDay = (d: number | null): string => (d == null ? '—' : new Date(d * 86_400_000).toISOString().slice(0, 10))
const fmtSec = (s: number | null): string => (s == null ? '—' : new Date(s * 1000).toISOString().slice(0, 10))
const fmtIdle = (d: number | null): string => (d == null ? '—' : d < 60 ? `${d}d` : d < 730 ? `${Math.round(d / 30)}mo` : `${(d / 365).toFixed(1)}y`)

/** A copy's storage-class mix as a thin bar (its tip: the bytes and list prices per class). */
function ClassBar({ c }: { c: DupCopy }) {
  const mix = classMix({ b: c.size, cb: c.cb })
  const rows = Object.entries(mix).filter(([, b]) => b > 0).sort((a, b) => b[1] - a[1])
  return (
    <Tooltip content={<ClassMixTip mix={mix} />}>
      <span className="dp-cls" aria-label={rows.map(([k, b]) => `${CLASS_NAMES[k] ?? k} ${Math.round((100 * b) / c.size)}%`).join(', ')}>
        {rows.map(([k, b]) => <i key={k} style={{ width: `${(100 * b) / c.size}%`, background: CLASS_COLORS[k] ?? 'var(--other)' }} />)}
      </span>
    </Tooltip>
  )
}

function Header({ data, tier, setBuckets }: { data: DupsResponse; tier: Tier; setBuckets: (b: string | undefined) => void }) {
  const { fmtBytes } = useUnits()
  const s = data.summary
  const m = pairMatrix(s.by_pair, tier)
  const max = Math.max(1, ...s.by_pair.filter(p => p.tier === tier).map(p => p.bytes))
  return (
    <section className="dp-head">
      <div className="dp-tiles">
        {s.net_by_tier.map(t => (
          <Tooltip key={t.tier} content={TIER_TIPS[t.tier]}>
            <div className="dp-tile">
              <div className="dp-tile-k">{t.tier}</div>
              <div className="dp-tile-v">{fmtBytes(t.reclaimable)}</div>
              <div className="dim">reclaimable · {fmtN(t.groups)} groups · {fmtN(t.copies)} copies</div>
            </div>
          </Tooltip>
        ))}
        {s.suspect.map(t => (
          <Tooltip key={`s-${t.tier}`} content={`Groups of ${s.suspect_copies}+ copies: mostly same-size collisions (e.g. shards named part-00000), not copies. Left out of the totals and the list unless "suspect" is on.`}>
            <div className="dp-tile dp-suspect">
              <div className="dp-tile-k">suspect {t.tier}</div>
              <div className="dp-tile-v">{fmtBytes(t.reclaimable)}</div>
              <div className="dim">{fmtN(t.groups)} groups · {fmtN(t.copies)} copies</div>
            </div>
          </Tooltip>
        ))}
      </div>
      <p className="dim dp-note">
        Net of nested copies (a copy under another group's copy counts once). Tiers overlap (a moved group can span two tree groups): never add them.
        Reclaimable = size × (copies − 1). Groups ≥ {fmtBytes(s.floor)}; {fmtN(s.checked_groups)} spot-checked.
      </p>
      {m.buckets.length > 0 && (
        <div className="dp-matrix-wrap">
          <table className="dp-matrix">
            <caption>{tier} copies by bucket pair <span className="dim">(click to filter)</span></caption>
            <thead><tr><th />{m.buckets.map(b => <th key={b}>{b}</th>)}</tr></thead>
            <tbody>
              {m.buckets.map(a => (
                <tr key={a}>
                  <th>{a}</th>
                  {m.buckets.map(b => {
                    const c = a <= b ? m.cell(a, b) : undefined
                    return (
                      <td key={b} className={c ? 'on' : undefined} style={c ? { '--w': `${Math.sqrt(c.bytes / max)}` } as React.CSSProperties : undefined}>
                        {c && (
                          <Tooltip content={<>{a === b ? <>≥2 copies in <b>{a}</b></> : <><b>{a}</b> ↔ <b>{b}</b></>}: {fmtN(c.groups)} groups, {fmtBytes(c.bytes)} (one copy's worth)</>}>
                            <button type="button" onClick={() => setBuckets(`${a},${b}`)}>{fmtBytes(c.bytes)}</button>
                          </Tooltip>
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function Copies({ g, checked, toggle, admin, day }: { g: DupGroup; checked: Set<string>; toggle: (p: string) => void; admin: boolean; day: number }) {
  const { fmtBytes } = useUnits()
  return (
    <table className="runs dp-copies">
      <thead>
        <tr>
          {admin && <th />}<th>copy</th><th className="num">size</th><th className="num">objects</th><th>created</th><th>last read</th>
          <th className="num">idle</th><th>class</th><th>check</th><th>staged</th>
        </tr>
      </thead>
      <tbody>
        {g.items.map(c => {
          const block = stageBlock(g, c)
          return (
            <tr key={c.path} className={c.keep ? 'dp-keep' : undefined}>
              {admin && (
                <td>
                  <Tooltip content={block ?? (c.keep ? 'The suggested keep (latest read, ties → oldest); check to stage it anyway' : 'Stage this copy')}>
                    <input type="checkbox" aria-label={`stage ${c.path}`} disabled={block != null} checked={checked.has(c.path)} onChange={() => toggle(c.path)} />
                  </Tooltip>
                </td>
              )}
              <td className="dp-path">
                <Link to={`/${c.path}`}>{c.path}</Link>
                {c.keep && <Tooltip content="The suggested copy to keep: the most recently read, ties → the oldest."><span className="dp-tag keep">keep</span></Tooltip>}
                {c.nested && <Tooltip content="Under another group's copy: its bytes are counted there."><span className="dp-tag">nested</span></Tooltip>}
              </td>
              <td className="num">{fmtBytes(c.size)}</td>
              <td className="num">{fmtN(c.n_files)}</td>
              <td className="nb">{fmtSec(c.created)}</td>
              <td className="nb">{c.last_read == null ? <span className="dim">never</span> : fmtDay(c.last_read)}</td>
              <td className="num">{fmtIdle(idleDays(c, day))}</td>
              <td><ClassBar c={c} /></td>
              <td>{c.check ? <Tooltip content={`${CHECK_TIPS[c.check]} ${fmtN(c.check_eq ?? 0)} / ${fmtN(c.check_n ?? 0)} objects equal.`}><span className={`dp-check ${c.check}`}>{c.check}</span></Tooltip> : <span className="dim">—</span>}</td>
              <td>{c.staged != null ? <Link to="/staged">plan {c.staged}</Link> : <span className="dim">—</span>}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export function DupsPage() {
  useDocTitle('Duplicates')
  const { fmtBytes } = useUnits()
  const admin = useIsAdmin()
  const [d] = useUrlState('d', stringParam())
  const [tier, setTier] = useUrlState('tier', enumParam<'' | Tier>('', ['', 'tree', 'moved']))
  const [min, setMin] = useUrlState('min', enumParam<string>('0', MINS.map(([, v]) => String(v))))
  const [buckets, setBuckets] = useUrlState('pair', stringParam())
  const [q, setQ] = useUrlState('q', stringParam())
  const [sort, setSort] = useUrlState('sort', enumParam<DupsSort>('reclaim', SORTS))
  const [suspect, setSuspect] = useUrlState('suspect', boolParam)
  const [nested, setNested] = useUrlState('nested', boolParam)
  const [qText, setQText] = useState(q ?? '')
  const dq = useDups({ d, tier, min: Number(min), buckets, q, suspect, nested })
  const data = dq.data
  const day = today()
  const groups = useMemo(() => (data ? sortGroups(data.groups, sort, day) : []), [data, sort, day])
  const [open, setOpen] = useState<Set<string>>(new Set())
  // The admin's changes to the suggested selection, by path (true: checked). Only an opened (reviewed) group's
  // copies are ever staged: a closed group's suggestion is shown as a count, never selected.
  const [picks, setPicks] = useState<Map<string, boolean>>(new Map())
  const checked = useMemo(() => {
    const out = new Set<string>()
    for (const g of groups) {
      if (!open.has(g.group)) continue
      const sug = new Set(suggested(g))
      for (const c of g.items) if ((picks.get(c.path) ?? sug.has(c.path)) && stageBlock(g, c) == null) out.add(c.path)
    }
    return out
  }, [groups, picks, open])
  const selected = groups.flatMap(g => g.items.filter(c => checked.has(c.path)).map(c => ({ ...c, kind: g.kind })))
  const selBytes = selected.reduce((s, c) => s + c.size, 0)
  const stage = useStage()
  const qc = useQueryClient()
  const toggle = (p: string) => setPicks(m => new Map(m).set(p, !checked.has(p)))
  const doStage = () => {
    if (!data || !selected.length) return
    setCurrentScan(data.date)
    stage.mutate(
      { prefixes: stageItems(selected, DEFAULT_STORE.scheme), note: `duplicate copies (/dups, scan ${data.date})` },
      { onSuccess: () => { setPicks(new Map()); void qc.invalidateQueries({ queryKey: ['dups'] }) } },
    )
  }
  const shownTier: Tier = tier || 'tree'
  return (
    <main className="staged-page scans-page dups-page">
      <SiteNav />
      <h1>Duplicates</h1>
      <p className="sub">
        Paths holding the same data in more than one place, from the scan's path index{data && <> (scan <code>{data.date}</code>)</>}.
        Review each group, keep one copy, and stage the rest; deletion then runs from <Link to="/staged">/staged</Link> as usual (dry run first).
      </p>
      {dq.isLoading ? <p>Loading…</p> : dq.error ? <p className="err">{dq.error.message}</p> : data && (<>
        <Header data={data} tier={shownTier} setBuckets={b => setBuckets(b)} />
        <div className="run-filters dp-filters">
          <label>tier <select value={tier} onChange={e => setTier(e.target.value as '' | Tier)}><option value="">all</option><option value="tree">tree</option><option value="moved">moved</option></select></label>
          <label>reclaimable <select value={min} onChange={e => setMin(e.target.value)}>{MINS.map(([l, v]) => <option key={v} value={String(v)}>{l}</option>)}</select></label>
          <label>sort <select value={sort} onChange={e => setSort(e.target.value as DupsSort)}><option value="reclaim">reclaimable</option><option value="size">size</option><option value="idle">idle</option></select></label>
          <form onSubmit={e => { e.preventDefault(); setQ(qText.trim() || undefined) }}>
            <input type="search" placeholder="path contains…" value={qText} onChange={e => setQText(e.target.value)} onBlur={() => setQ(qText.trim() || undefined)} />
          </form>
          {buckets && <button type="button" className="dp-chip" onClick={() => setBuckets(undefined)} aria-label="clear the bucket filter">{buckets.replace(',', ' ↔ ')} ✕</button>}
          <Tooltip content={`Include groups of ${data.summary.suspect_copies}+ copies (mostly same-size collisions).`}><label><input type="checkbox" checked={suspect} onChange={e => setSuspect(e.target.checked)} /> suspect</label></Tooltip>
          <Tooltip content="Include groups whose every copy sits under another group's copy."><label><input type="checkbox" checked={nested} onChange={e => setNested(e.target.checked)} /> nested</label></Tooltip>
          <span className="dim">{fmtN(groups.length)}{data.more ? '+' : ''} groups</span>
        </div>
        {admin && (
          <div className="dp-stagebar">
            <button type="button" disabled={!selected.length || stage.isPending} onClick={doStage}>
              {stage.isPending ? 'Staging…' : `Stage ${fmtN(selected.length)} cop${selected.length === 1 ? 'y' : 'ies'} (${fmtBytes(selBytes)})`}
            </button>
            <span className="dim">Open a group to review it: every copy but its keep is pre-checked (a moved copy only once its spot check passed). Only open groups' checked copies are staged, into the open plan; nothing is deleted here.</span>
            {stage.error && <span className="err">{stage.error.message}</span>}
            {stage.data && <span>Staged {fmtN(stage.data.staged.length + (stage.data.staged_objects?.length ?? 0))} — <Link to="/staged">review on /staged</Link></span>}
          </div>
        )}
        <div className="runs-wrap">
          <table className="runs dp-groups">
            <thead>
              <tr><th /><th>shared path</th><th>tier</th><th className="num">size</th><th className="num">copies</th><th className="num">reclaimable</th><th className="num">idle</th>{admin && <th className="num">to stage</th>}</tr>
            </thead>
            <tbody>
              {groups.map(g => {
                const isOpen = open.has(g.group)
                const flip = () => setOpen(s => { const n = new Set(s); if (n.has(g.group)) n.delete(g.group); else n.add(g.group); return n })
                const nChecked = g.items.filter(c => checked.has(c.path)).length
                const keep = g.items.find(c => c.keep)
                const names = new Set(g.items.map(c => c.rel)).size
                return (
                  <Fragment key={g.group}>
                    <tr className={'dp-group' + (isOpen ? ' open' : '')} onClick={flip}>
                      <td><button type="button" className="dp-caret" aria-expanded={isOpen} aria-label={isOpen ? 'collapse' : 'expand'} onClick={e => { e.stopPropagation(); flip() }}>{isOpen ? '▾' : '▸'}</button></td>
                      <td className="dp-path">{g.tier === 'moved' && keep ? keep.rel : g.rel}{g.kind === 'dir' ? '/' : ''} {g.tier === 'moved' && <span className="dim">+{names - 1} other name{names === 2 ? '' : 's'}</span>}</td>
                      <td><Tooltip content={TIER_TIPS[g.tier]}><span className={`dp-tier ${g.tier}`}>{g.tier}</span></Tooltip>{g.suspect && <span className="dp-tag warn">suspect</span>}</td>
                      <td className="num">{fmtBytes(g.size)}</td>
                      <td className="num"><Tooltip content={[...new Set(g.items.map(c => c.bucket))].join(', ')}><span>{g.copies}</span></Tooltip></td>
                      <td className="num">{fmtBytes(g.reclaimable)}</td>
                      <td className="num">{fmtIdle(groupIdle(g, day))}</td>
                      {admin && <td className="num">{isOpen ? nChecked : <Tooltip content="Suggested; open the group to review and select them."><span className="dim">{suggested(g).length}</span></Tooltip>}</td>}
                    </tr>
                    {isOpen && <tr className="dp-detail"><td /><td colSpan={admin ? 7 : 6}><Copies g={g} checked={checked} toggle={toggle} admin={admin} day={day} /></td></tr>}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
        {!groups.length && <p className="dim">No groups match.</p>}
        {data.more && <p className="dim">Showing the first {fmtN(groups.length)} groups; narrow the filters to see others.</p>}
      </>)}
      <SiteKbd />
    </main>
  )
}
