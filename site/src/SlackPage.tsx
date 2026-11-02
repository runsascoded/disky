import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { stringParam, useUrlState } from 'use-prms'
import { SiteNav } from './SiteNav'
import { useDocTitle } from './title'
import { DEFAULT_PARAMS, type DigestParams, imageRequest, opText, type PreviewBundle } from './digest'
import { decodeParams, encodeParams } from './digest/codec'
import { type Tok, tokenize } from './digest/mrkdwn'
import './slack.scss'

// /admin/slack (specs/slack-preview-page.md), admin only: the monthly digest's OP as the job posted it (its preview
// bundle, `/api/digest-preview`), beside the same rows re-rendered under other params (`src/digest`, the job's
// renderer ported byte for byte) with their plots drawn by `/api/digest-image`. Read-only: nothing here changes what
// the job posts. Candidates live in the URL (`?c=<params>;<params>…`, `digest/codec`), so a comparison is a link.

interface Listing { bundles: { key: string; month: string; channel: string; variant: string }[] }

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(r.status === 401 ? 'unauthenticated' : r.status === 403 ? 'admins only' : `HTTP ${r.status}: ${await r.text()}`)
  return (await r.json()) as T
}

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v)
  useEffect(() => {
    const t = setTimeout(() => setD(v), ms)
    return () => clearTimeout(t)
  }, [v, ms])
  return d
}

/** A plot request drawn by the site (`/api/digest-image`), as an object URL; debounced, one render per distinct request. */
function usePlot(req: Record<string, unknown>) {
  const body = useDebounced(JSON.stringify(req))
  return useQuery({
    queryKey: ['digest-image', body],
    staleTime: Infinity,
    queryFn: async () => {
      const r = await fetch('/api/digest-image', { method: 'POST', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body })
      if (!r.ok) throw new Error(`plot: HTTP ${r.status}: ${await r.text()}`)
      return URL.createObjectURL(await r.blob())
    },
  })
}

const PLOT = '<plot>'
/** A line, its image's URL normalized (the posted OP names the hosted plot, a candidate the placeholder). */
const norm = (l: string) => l.replace(/^(!\[[^\]]*\])\(.*\)$/, `$1(${PLOT})`)

function Toks({ toks, plot }: { toks: Tok[]; plot: ReactNode }) {
  return <>{toks.map((k, i) => {
    switch (k.t) {
      case 'text': return <span key={i}>{k.s}</span>
      case 'bold': return <b key={i}>{k.s}</b>
      case 'italic': return <i key={i}>{k.s}</i>
      case 'link': return <a key={i} href={k.href} target="_blank" rel="noreferrer">{k.s}</a>
      case 'arrow': return <span key={i} className={`arrow ${k.deg > 0 ? 'up' : k.deg < 0 ? 'down' : 'flat'}`} style={{ transform: `rotate(${-k.deg}deg)` }} title={`arrow_deg${k.deg}`}>➜</span>
      case 'image': return <span key={i} className="plot">{plot}</span>
    }
  })}</>
}

/** An OP as Slack shows it; `live`'s lines given, the lines not among them are marked. */
function Op({ text, plotReq, live }: { text: string; plotReq: Record<string, unknown>; live?: string[] }) {
  const plot = usePlot(plotReq)
  const img = plot.error ? <span className="err">{plot.error.message}</span> : plot.data ? <img src={plot.data} alt="plot" /> : <span className="loading">drawing…</span>
  const seen = new Set(live)
  return (
    <div className="slack-op">
      {text.split('\n').map((l, i) => (
        <div key={i} className={`line${live && !seen.has(norm(l)) ? ' changed' : ''}${l ? '' : ' blank'}`}><Toks toks={tokenize(l)} plot={img} /></div>
      ))}
    </div>
  )
}

const HEADLINES: [string, DigestParams['headline']][] = [['mtd · window', ['mtd', 'window']], ['window · mtd', ['window', 'mtd']], ['mtd only', ['mtd']], ['window only', ['window']], ['none', []]]

function Controls({ p, set }: { p: DigestParams; set: (p: DigestParams) => void }) {
  const num = (v: string, lo: number, hi: number) => Math.min(hi, Math.max(lo, Number(v) || lo))
  return (
    <div className="slack-controls">
      <label>window <input type="number" min={1} max={92} value={p.windowDays} onChange={e => set({ ...p, windowDays: num(e.target.value, 1, 92) })} /> d</label>
      <label>today at <input type="number" min={0.05} max={1} step={0.05} value={p.todayAt[0]} onChange={e => set({ ...p, todayAt: [Math.min(num(e.target.value, 0.05, 1), p.todayAt[1]), p.todayAt[1]] })} />
        → <input type="number" min={0.05} max={1} step={0.05} value={p.todayAt[1]} onChange={e => set({ ...p, todayAt: [p.todayAt[0], Math.max(num(e.target.value, 0.05, 1), p.todayAt[0])] })} /></label>
      <label>diff <select value={p.diff} onChange={e => set({ ...p, diff: e.target.value as DigestParams['diff'] })}>
        <option value="window">window</option><option value="mtd">month-to-date</option></select></label>
      <label>headline <select value={p.headline.join()} onChange={e => set({ ...p, headline: HEADLINES.find(([, h]) => h.join() === e.target.value)![1] })}>
        {HEADLINES.map(([l, h]) => <option key={l} value={h.join()}>{l}</option>)}</select></label>
      <label>weeks <select value={p.weeks} onChange={e => set({ ...p, weeks: e.target.value as DigestParams['weeks'] })}>
        <option value="window">window's</option><option value="month">month's</option></select></label>
    </div>
  )
}

function Bundle({ b, cands, setCands }: { b: PreviewBundle; cands: string[]; setCands: (c: string[]) => void }) {
  const links = { plotUrl: PLOT, prevUrl: b.prev_url }
  const rerender = opText(b.rows, b.month, b.cfg, { plotUrl: b.plot_url === null ? null : PLOT, prevUrl: b.prev_url })
  const matches = rerender.split('\n').map(norm).join('\n') === b.op.split('\n').map(norm).join('\n')
  const live = b.op.split('\n').map(norm)
  return <>
    <p className="dim">
      Posted {b.posted_at} to <code>{b.channel}</code> ({b.variant}){b.permalink && <> · <a href={b.permalink} target="_blank" rel="noreferrer">the thread</a></>}.
      {' '}{matches ? 'The site\'s renderer reproduces it exactly.' : <span className="err">The site's renderer no longer reproduces it (the job or the renderer changed since).</span>}
    </p>
    <div className="slack-cols">
      <section className="slack-col live">
        <h2>Live</h2>
        <Op text={b.op} plotReq={b.image} />
      </section>
      {cands.map((c, i) => {
        const p = decodeParams(c)
        const set = (q: DigestParams) => setCands(cands.map((x, j) => (j === i ? encodeParams(q) : x)))
        return (
          <section key={i} className="slack-col">
            <h2>Candidate {i + 1} <button onClick={() => set(DEFAULT_PARAMS)} disabled={!c}>reset</button>
              <button onClick={() => setCands(cands.filter((_, j) => j !== i))}>remove</button></h2>
            <Controls p={p} set={set} />
            <Op text={opText(b.rows, b.month, b.cfg, links, p)} plotReq={imageRequest(b.rows, b.month, b.cfg, p)} live={live} />
          </section>
        )
      })}
      {cands.length < 3 && <button className="slack-add" onClick={() => setCands([...cands, cands[cands.length - 1] ?? ''])}>+ compare</button>}
    </div>
    <details className="slack-rows">
      <summary>Rows ({b.rows.template})</summary>
      <pre>{JSON.stringify(b.rows, null, 1)}</pre>
    </details>
  </>
}

export function SlackPage() {
  useDocTitle('Slack digest', 'Admin')
  const [k, setK] = useUrlState('k', stringParam())
  const [c, setC] = useUrlState('c', stringParam())
  const list = useQuery({ queryKey: ['digest-preview'], queryFn: () => getJson<Listing>('/api/digest-preview') })
  const key = k ?? list.data?.bundles[0]?.key
  const b = useQuery({ queryKey: ['digest-preview', key], enabled: !!key, queryFn: () => getJson<PreviewBundle>(`/api/digest-preview?key=${encodeURIComponent(key!)}`) })
  // a default candidate is `.` in the URL (its params encode empty); no `c` at all: one default candidate
  const cands = c === undefined ? [''] : c ? c.split(';').map(x => (x === '.' ? '' : x)) : []
  return (
    <div className="admin-page slack-page">
      <SiteNav />
      <p className="crumbs"><Link to="/admin">admin</Link> · <Link to="/admin/usage">usage</Link></p>
      <h1>Slack digest</h1>
      <p className="dim">
        The month thread's OP as the job last posted it, beside the same scans re-rendered under other parameters.
        Nothing here changes what the job posts.
      </p>
      {list.error && <p className="err">{list.error.message}</p>}
      {list.data && !list.data.bundles.length && <p>No preview bundles yet: the job writes one per converge once its digest config sets <code>preview</code>.</p>}
      {list.data && list.data.bundles.length > 0 && (
        <div className="usage-filters">
          <label>thread <select value={key} onChange={e => setK(e.target.value)}>
            {list.data.bundles.map(x => <option key={x.key} value={x.key}>{x.month} · {x.channel} · {x.variant}</option>)}
          </select></label>
        </div>
      )}
      {b.error && <p className="err">{b.error.message}</p>}
      {b.isLoading && key && <p className="loading">Loading…</p>}
      {b.data && <Bundle b={b.data} cands={cands} setCands={x => setC(x.map(y => y || '.').join(';'))} />}
    </div>
  )
}
