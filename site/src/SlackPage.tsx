import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type ReactNode, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { stringParam, useUrlState } from 'use-prms'
import { SiteNav } from './SiteNav'
import { useDocTitle } from './title'
import { DEFAULT_PARAMS, type DigestParams, imageRequest, opText, type CwRows, type PreviewBundle, type PreviewReply, type RenderedReply, replies as renderReplies } from './digest'
import { decodeParams, encodeParams, jobParams, previewMarks } from './digest/codec'
import { type Tok, tokenize } from './digest/mrkdwn'
import './slack.scss'

// /admin/slack (specs/slack-preview-page.md), admin only: the monthly digest's OP as the job posted it (its preview
// bundle, `/api/digest-preview`), beside the same rows re-rendered under other params (`src/digest`, the job's
// renderer ported byte for byte) with their plots drawn by `/api/digest-image`. Candidates live in the URL
// (`?dc=<params>;<params>…`, `digest/codec`), so a comparison is a link. Two panels act (specs/digest-runway-marks.md
// §4): the live settings the job renders under (`/api/digest-settings`, D1), and re-applying the digest to chosen
// messages of the thread (`/api/digest-reapply`: a Batch job running the CLI's selectors, a dry run first).

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

const ARROW_POINTS = '10,39 58,39 58,23 90,50 58,77 58,61 10,61'

/** A candidate `ArrowMark`: the trend avatars' arrow at `deg`, bare in `color` or white on a `color` disc / chip. */
function PoolMarkSvg({ form, deg, color }: { form: 'bare' | 'disc' | 'chip'; deg: number; color: string }) {
  const arrow = form === 'bare' ? 1 : form === 'disc' ? 0.72 : 0.8
  return (
    <svg className="pool-mark" viewBox="0 0 100 100" aria-label={`${form} ${deg}°`}>
      {form === 'disc' && <circle cx="50" cy="50" r="50" fill={color} />}
      {form === 'chip' && <rect width="100" height="100" rx="20" fill={color} />}
      <polygon points={ARROW_POINTS} fill={form === 'bare' ? color : '#fff'} transform={`rotate(${-deg} 50 50) translate(50 50) scale(${arrow}) translate(-50 -50)`} />
    </svg>
  )
}

function Toks({ toks, plot }: { toks: Tok[]; plot: ReactNode }) {
  return <>{toks.map((k, i) => {
    switch (k.t) {
      case 'text': return <span key={i}>{k.s}</span>
      case 'bold': return <b key={i}>{k.s}</b>
      case 'italic': return <i key={i}>{k.s}</i>
      case 'link': return <a key={i} href={k.href} target="_blank" rel="noreferrer">{k.s}</a>
      case 'arrow': return <span key={i} className={`arrow ${k.deg > 0 ? 'up' : k.deg < 0 ? 'down' : 'flat'}`} style={{ transform: `rotate(${-k.deg}deg)` }} title={`arrow_deg${k.deg}`}>➜</span>
      case 'emoji': return <span key={i} title={`:${k.name}:`}>{k.char}</span>
      case 'hue': return <span key={i} className={`hue ${k.shape}`} title={`:${k.name}:`} style={{ background: k.color }} />
      case 'pool': return <PoolMarkSvg key={i} form={k.form} deg={k.deg} color={k.color} />
      case 'image': return <span key={i} className="plot">{plot}</span>
    }
  })}</>
}

/** A plot at full size over the page; a click or Escape closes it. */
function Lightbox({ src, close }: { src: string; close: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [close])
  return <div className="slack-lightbox" onClick={close} role="dialog" aria-label="plot"><img src={src} alt="plot" /></div>
}

/** An OP as Slack shows it; `live`'s lines given, the lines not among them are marked. The plot opens in a lightbox. */
function Op({ text, plotReq, live }: { text: string; plotReq: Record<string, unknown>; live?: string[] }) {
  const plot = usePlot(plotReq)
  const [big, setBig] = useState(false)
  const img = plot.error ? <span className="err">{plot.error.message}</span>
    : plot.data ? <><img src={plot.data} alt="plot" className="zoomable" onClick={() => setBig(true)} />{big && <Lightbox src={plot.data} close={() => setBig(false)} />}</>
    : <span className="loading">drawing…</span>
  const seen = new Set(live)
  return (
    <div className="slack-op">
      {text.split('\n').map((l, i) => (
        <div key={i} className={`line${live && !seen.has(norm(l)) ? ' changed' : ''}${l ? '' : ' blank'}`}><Toks toks={tokenize(l)} plot={img} /></div>
      ))}
    </div>
  )
}

/** A reply as Slack shows it: its sender (name + avatar, or emoji icon) over its body. */
function Reply({ r, changed }: { r: PreviewReply | RenderedReply; changed?: boolean }) {
  const icon = r.icon_url ? <img src={r.icon_url} alt="" /> : <span className="emoji-icon"><Toks toks={tokenize(r.icon_emoji ?? '')} plot={null} /></span>
  return (
    <div className={`slack-reply${changed ? ' changed' : ''}`}>
      <div className="avi">{icon}</div>
      <div><div className="sender"><Toks toks={tokenize(r.username)} plot={null} /></div>
        <div className="body"><Toks toks={tokenize(r.body)} plot={null} /></div></div>
    </div>
  )
}

/** The month's replies, newest first: the latest few, the rest behind a toggle. */
function Replies({ replies, live }: { replies: (PreviewReply | RenderedReply)[]; live?: PreviewReply[] }) {
  const [all, setAll] = useState(false)
  const shown = all ? replies : replies.slice(0, REPLIES_SHOWN)
  return (
    <div className="slack-replies">
      <h3>Replies <span className="dim">({replies.length}, newest first)</span></h3>
      {shown.map(r => {
        const l = live?.find(x => x.key === r.key)
        return <Reply key={r.key} r={r} changed={!!live && (l?.username !== r.username || l?.body !== r.body || l?.icon_url !== r.icon_url)} />
      })}
      {replies.length > REPLIES_SHOWN && <button onClick={() => setAll(!all)}>{all ? 'fewer' : `all ${replies.length}`}</button>}
    </div>
  )
}
const REPLIES_SHOWN = 4

const HEADLINES: [string, DigestParams['headline']][] = [['mtd · window', ['mtd', 'window']], ['window · mtd', ['window', 'mtd']], ['mtd only', ['mtd']], ['window only', ['window']], ['none', []]]

const ARROW_MARK_OPTIONS = <>
  <option value="arrow">A: arrow, runway color (preview)</option>
  <option value="arrow-disc">B: white arrow on runway disc (preview)</option>
  <option value="arrow-chip">C: white arrow on runway square (preview)</option>
  <option value="arrow-circle">D: trend arrow + runway circle (preview)</option>
</>
const POOL_MARK_OPTIONS = <>
  <option value="runway">🔴🟠🟡🟢 by runway</option><option value="runway-hue">:runway-1…8: by runway</option>{ARROW_MARK_OPTIONS}
</>

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
      <label>layout <select value={p.layout} onChange={e => set({ ...p, layout: e.target.value as DigestParams['layout'] })}>
        <option value="primary">primary + others</option>
        <option value="pools">a line per pool</option>
      </select></label>
      <label>quotas <select value={p.quotas} onChange={e => set({ ...p, quotas: e.target.value as DigestParams['quotas'] })}>
        <option value="off">off</option><option value="circles">line, 🔴🟠🟡🟢 by fill</option><option value="runway">line, 🔴🟠🟡🟢 by runway</option><option value="runway-hue">line, :runway-1…8: by runway</option>{ARROW_MARK_OPTIONS}</select></label>
      <label>plot <select value={p.plot} onChange={e => set({ ...p, plot: e.target.value as DigestParams['plot'] })}>
        <option value="usage">usage vs quota</option><option value="pools">% of each quota</option></select></label>
      <label>red at <input type="number" min={1} max={100} value={p.alertAt} onChange={e => set({ ...p, alertAt: num(e.target.value, 1, 100) })} />%</label>
      <label>reply marks <select value={p.replyMarks ?? ''} onChange={e => set({ ...p, replyMarks: (e.target.value || undefined) as DigestParams['replyMarks'] })}>
        <option value="">as rendered</option>
        <option value="warning">⚠️ over the alert share</option><option value="circles">🔴🟠🟡🟢 by fill</option>{POOL_MARK_OPTIONS}</select></label>
      <label>reply headline <select value={p.replyTotal === undefined ? '' : p.replyTotal ? 'total' : 'primary'} onChange={e => set({ ...p, replyTotal: e.target.value === '' ? undefined : e.target.value === 'total' })}>
        <option value="">as rendered</option><option value="primary">primary</option><option value="total">every bucket</option></select></label>
    </div>
  )
}

interface Settings { template: string; params: string; quota_marks: string; updated_by: string; updated_at: number }
interface Run { job_id: string; month: string; channel: string; target: string; dry_run: boolean; params: string | null; quota_marks: string | null; actor: string; ts: number }
interface RunDetail { run: Run; status: string | null; output: string | null }

const MARKS = ['warning', 'circles', 'runway', 'runway-hue']
const DONE = ['SUCCEEDED', 'FAILED', 'DELETION_IN_PROGRESS']
const when = (ts: number) => new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z'

async function send<T>(url: string, method: string, body: unknown): Promise<T> {
  const r = await fetch(url, { method, credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
  const j = await r.json().catch(() => ({})) as T & { error?: string }
  if (!r.ok) throw new Error(j.error ?? `HTTP ${r.status}`)
  return j
}

/** A button that asks once more before it acts (no browser dialogs). */
function Confirm({ label, confirm, onClick, disabled }: { label: string; confirm: string; onClick: () => void; disabled?: boolean }) {
  const [armed, setArmed] = useState(false)
  // Armed long enough to read the prompt (e.g. "edit thread in D0CE8QE83EH?") before confirming.
  useEffect(() => { if (armed) { const t = setTimeout(() => setArmed(false), 15000); return () => clearTimeout(t) } }, [armed])
  return <button disabled={disabled} className={armed ? 'armed' : ''} onClick={() => (armed ? (setArmed(false), onClick()) : setArmed(true))}>{armed ? confirm : label}</button>
}

/** The live settings: what the job renders the OP (`params`) and replies (`quota_marks`) under, over its file's. */
function LiveSettings({ b, cands, form, setForm }: { b: PreviewBundle; cands: string[]; form: { params: string; quota_marks: string }; setForm: (f: { params: string; quota_marks: string }) => void }) {
  const qc = useQueryClient()
  const template = b.cfg.template
  const s = useQuery({ queryKey: ['digest-settings', template], queryFn: () => getJson<{ settings: Settings | null }>(`/api/digest-settings?template=${template}`) })
  const save = useMutation({
    mutationFn: () => send<{ settings: Settings }>('/api/digest-settings', 'PUT', { template, ...form }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['digest-settings', template] }),
  })
  const cur = s.data?.settings
  // the form starts from the live row (none: the bundle's params; reply marks unknown to the page until set)
  const [seeded, setSeeded] = useState(false)
  useEffect(() => {
    if (!seeded && cur) { setForm({ params: cur.params, quota_marks: cur.quota_marks }); setSeeded(true) }
  }, [cur, seeded, setForm])
  return (
    <section className="slack-act">
      <h2>Live settings</h2>
      <p className="dim">
        {s.isLoading ? 'Loading…' : cur
          ? <>The job renders under params <code>{cur.params || '(defaults)'}</code> and quota marks <code>{cur.quota_marks}</code> (set by {cur.updated_by}, {when(cur.updated_at)}).</>
          : <>No live settings: the job uses its config file's (this bundle was rendered under params <code>{b.params || '(defaults)'}</code>).</>}
        {' '}A change applies from the next converge (the next scan, or a re-apply below).
      </p>
      {s.error && <p className="err">{s.error.message}</p>}
      <div className="slack-controls">
        <label>OP params <input className="wide" value={form.params} onChange={e => setForm({ ...form, params: e.target.value })} placeholder="(defaults)" /></label>
        {cands.map((c, i) => {
          const op = jobParams(c)
          const p = decodeParams(c)
          return previewMarks(p) ? <span key={i} className="dim">candidate {i + 1}: preview marks, no emoji yet</span>
            : <button key={i} onClick={() => setForm({ ...form, params: op })} disabled={form.params === op}>candidate {i + 1}'s</button>
        })}
        <label>reply marks <select value={form.quota_marks} onChange={e => setForm({ ...form, quota_marks: e.target.value })}>
          {MARKS.map(m => <option key={m} value={m}>{m}</option>)}</select></label>
        <Confirm label="save" confirm={`save as live (${template})?`} disabled={save.isPending || (cur?.params === form.params && cur?.quota_marks === form.quota_marks)} onClick={() => save.mutate()} />
      </div>
      {save.error && <p className="err">{save.error.message}</p>}
    </section>
  )
}

/** Re-apply the digest to chosen messages of the thread: a dry run lists what would change; apply edits exactly those. */
function Reapply({ b, form }: { b: PreviewBundle; form: { params: string; quota_marks: string } }) {
  const qc = useQueryClient()
  const [channel, setChannel] = useState(b.channel)
  const [kind, setKind] = useState<'op' | 'days' | 'thread'>('op')
  const [from, setFrom] = useState(`${b.month}-01`)
  const [to, setTo] = useState(`${b.month}-01`)
  const [underForm, setUnderForm] = useState(true)
  const [job, setJob] = useState<string | null>(null)
  const target = kind === 'days' ? (from === to ? from : `${from}..${to}`) : kind
  const runs = useQuery({ queryKey: ['digest-reapply'], queryFn: () => getJson<{ runs: Run[] }>('/api/digest-reapply') })
  const detail = useQuery({
    queryKey: ['digest-reapply', job], enabled: !!job,
    queryFn: () => getJson<RunDetail>(`/api/digest-reapply?job=${job}`),
    refetchInterval: q => (q.state.data && DONE.includes(q.state.data.status ?? '') ? false : 5000),
  })
  const go = useMutation({
    mutationFn: (dry: boolean) => send<{ run: Run }>('/api/digest-reapply', 'POST', {
      month: b.month, channel, target, dry_run: dry, ...(underForm ? { params: form.params, quota_marks: form.quota_marks } : {}),
    }),
    onSuccess: ({ run }) => { setJob(run.job_id); qc.invalidateQueries({ queryKey: ['digest-reapply'] }) },
  })
  const days = { min: `${b.month}-01`, max: `${b.month}-31` }
  return (
    <section className="slack-act">
      <h2>Re-apply</h2>
      <p className="dim">Re-renders exactly the chosen messages of {b.month}'s thread and edits them in place (no new replies, no alert); a whole-thread run converges as the cron does. Runs as a Batch job (~2 min). Preview first.</p>
      <div className="slack-controls">
        <label>channel <input className="wide" value={channel} onChange={e => setChannel(e.target.value.trim())} /></label>
        <label>what <select value={kind} onChange={e => setKind(e.target.value as typeof kind)}>
          <option value="op">the OP</option><option value="days">replies of days…</option><option value="thread">the whole thread</option></select></label>
        {kind === 'days' && <>
          <input type="date" {...days} value={from} onChange={e => { setFrom(e.target.value); if (e.target.value > to) setTo(e.target.value) }} />
          → <input type="date" {...days} value={to} min={from} onChange={e => setTo(e.target.value)} />
        </>}
        <label><input type="checkbox" checked={underForm} onChange={e => setUnderForm(e.target.checked)} /> under the form's settings above (else the live ones)</label>
        <button disabled={go.isPending} onClick={() => go.mutate(true)}>preview (dry run)</button>
        <Confirm label="apply" confirm={`edit ${target} in ${channel}?`} disabled={go.isPending} onClick={() => go.mutate(false)} />
      </div>
      {go.error && <p className="err">{go.error.message}</p>}
      {runs.data && runs.data.runs.length > 0 && (
        <ul className="slack-runs">
          {runs.data.runs.map(r => (
            <li key={r.job_id}><button className={job === r.job_id ? 'on' : ''} onClick={() => setJob(r.job_id)}>{when(r.ts)}</button>
              {' '}{r.dry_run ? 'preview' : <b>apply</b>} {r.target} in <code>{r.channel}</code>
              {(r.params !== null || r.quota_marks !== null) && <> under <code>{r.params || '(defaults)'}</code> / <code>{r.quota_marks}</code></>} · {r.actor}</li>
          ))}
        </ul>
      )}
      {job && detail.data && <div className="slack-run">
        <p className="dim"><code>{job}</code>: {detail.data.status ?? 'status unknown'}{!DONE.includes(detail.data.status ?? '') && ' …'}</p>
        <pre>{detail.data.output ?? '(no output yet)'}</pre>
      </div>}
      {detail.error && <p className="err">{detail.error.message}</p>}
    </section>
  )
}

function Bundle({ b, cands, setCands }: { b: PreviewBundle; cands: string[]; setCands: (c: string[]) => void }) {
  const links = { plotUrl: PLOT, prevUrl: b.prev_url }
  // the bundle's params (absent or '' = the defaults) are what the OP was rendered under
  const posted = b.params ?? ''
  const rerender = opText(b.rows, b.month, b.cfg, { plotUrl: b.plot_url === null ? null : PLOT, prevUrl: b.prev_url }, decodeParams(posted))
  const matches = rerender.split('\n').map(norm).join('\n') === b.op.split('\n').map(norm).join('\n')
  const live = b.op.split('\n').map(norm)
  const [form, setForm] = useState({ params: posted, quota_marks: 'runway' })
  const replyCfg = b.rows.template === 'cw' ? b.reply : null
  const candReplies = (p: DigestParams) => replyCfg ? renderReplies(b.rows as CwRows, b.cfg, replyCfg, b.variant, { marks: p.replyMarks, total: p.replyTotal }) : null
  const rendered = candReplies(DEFAULT_PARAMS)
  const repliesMatch = !!rendered && JSON.stringify(rendered) === JSON.stringify(b.replies)
  return <>
    <p className="dim">
      {b.op_ts ? 'Posted' : 'Never posted: preview only, written'} {b.posted_at} {b.op_ts ? 'to' : 'as'} <code>{b.channel}</code> ({b.variant}){b.permalink && <> · <a href={b.permalink} target="_blank" rel="noreferrer">the thread</a></>}.
      {posted && <> Rendered under params <code>{posted}</code>.</>}
      {' '}{matches ? 'The site\'s renderer reproduces it exactly.' : <span className="err">The site's renderer no longer reproduces it (the job or the renderer changed since).</span>}
      {rendered && (repliesMatch ? ' Its replies too.' : <span className="err"> The site's renderer no longer reproduces its replies.</span>)}
    </p>
    <div className="slack-cols">
      <section className="slack-col live">
        <h2>Live</h2>
        <Op text={b.op} plotReq={b.image} />
        {b.replies?.length ? <Replies replies={b.replies} /> : null}
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
            {replyCfg ? <Replies replies={candReplies(p)!} live={b.replies} />
              : b.rows.template === 'cw' && <p className="dim">This bundle predates re-rendered replies; the next scan's will have them.</p>}
          </section>
        )
      })}
      {cands.length < 3 && <button className="slack-add" onClick={() => setCands([...cands, cands[cands.length - 1] ?? ''])}>+ compare</button>}
    </div>
    {/* a never-posted bundle's params are a try-out's: not seeded into the panels that change the live thread */}
    {b.op_ts && <>
      <LiveSettings b={b} cands={cands} form={form} setForm={setForm} />
      <Reapply b={b} form={form} />
    </>}
    <details className="slack-rows">
      <summary>Rows ({b.rows.template})</summary>
      <pre>{JSON.stringify(b.rows, null, 1)}</pre>
    </details>
  </>
}

export function SlackPage() {
  useDocTitle('Slack digest', 'Admin')
  const [k, setK] = useUrlState('dk', stringParam())
  const [c, setC] = useUrlState('dc', stringParam())
  const list = useQuery({ queryKey: ['digest-preview'], queryFn: () => getJson<Listing>('/api/digest-preview') })
  const key = k ?? list.data?.bundles[0]?.key
  const b = useQuery({ queryKey: ['digest-preview', key], enabled: !!key, queryFn: () => getJson<PreviewBundle>(`/api/digest-preview?b=${encodeURIComponent(key!)}`) })
  // a default candidate is `.` in the URL (its params encode empty); no `dc` at all: one default candidate
  const cands = c === undefined ? [''] : c ? c.split(';').map(x => (x === '.' ? '' : x)) : []
  return (
    <div className="admin-page slack-page">
      <SiteNav />
      <p className="crumbs"><Link to="/admin">admin</Link> · <Link to="/admin/usage">usage</Link></p>
      <h1>Slack digest</h1>
      <p className="dim">
        The month thread's OP as the job last posted it, beside the same scans re-rendered under other parameters.
        Below them, the live settings the job renders under, and re-applying the digest to chosen messages.
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
