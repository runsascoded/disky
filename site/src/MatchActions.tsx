// The controls of an action on a filter's matches (`matchAct.ts`'s states, drawn): offered at once; the
// clicked control turns into a spinner (listing, one batch) or a bar (several batches), then the accepted
// state — or, when the matches can't be acted on here, the reason, muted, in its place. One live region
// per control announces each state; the box keeps its width (`.mact`), so a row never jumps.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { FaRegTrashCan } from 'react-icons/fa6'
import { getCurrentScan, useOwnerMutations } from './owners'
import { call } from './plans'
import { Tooltip } from './Tooltip'
import { AssignSelect } from './AssignSelect'
import { type Resolved, type Targets, targetsText } from './filterCover'
import { type ActController, actController, type ActDeps, type ActReq, type ActState, bucketsIn, caveats, ROW_CONFIRM_OVER, targetsOf } from './matchAct'
import { chunks, ASSIGN_CHUNK, STAGE_CHUNK } from './batches'
import { elideMid } from './CopyName'

/** A row's hover dwell before its matches are fetched (focus and the controls' own hover start at once). */
export const ROW_DWELL_MS = 150

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

/** The app's send paths: the owner ledger's POST (it refreshes the ledger), the plan API (then the plans). */
export function useActDeps(): Pick<ActDeps, 'assign' | 'call' | 'asOf' | 'landed'> {
  const { post } = useOwnerMutations()
  const qc = useQueryClient()
  return {
    assign: a => post.mutateAsync(a),
    call,
    asOf: getCurrentScan,
    landed: k => { if (k === 'stage') void qc.invalidateQueries({ queryKey: ['plans'] }) },
  }
}

/** One action's state and controller; back to idle when `resetKey` changes (another filter, scan, path). */
export function useMatchAct(deps: ActDeps, resetKey?: unknown): ActController & { state: ActState; confirmT: Targets | null } {
  const [state, setState] = useState<ActState>({ s: 'idle' })
  const cell = useRef<ActState>(state)
  const depsRef = useRef(deps)
  depsRef.current = deps
  const ctl = useMemo(() => actController(() => depsRef.current, s => { cell.current = s; setState(s) }, () => cell.current), [])
  useEffect(() => { if (cell.current.s !== 'idle') { cell.current = { s: 'idle' }; setState(cell.current) } }, [resetKey])
  // The confirm's targets are recomputed each render: the bulk bar's review can untick items while it asks.
  const confirmT = state.s === 'confirm' ? targetsOf(state.got, state.req, deps) : null
  return { ...ctl, state, confirmT }
}

/** Keep keyboard focus inside a control whose focused button a state change removed (trash → spinner →
 *  accepted): the next control in the box, else the box itself (`tabIndex=-1`). */
export function useKeepFocus(state: ActState['s']) {
  const ref = useRef<HTMLSpanElement>(null)
  const inside = useRef(false)
  useEffect(() => {
    const el = ref.current
    if (!el || !inside.current) return
    const a = document.activeElement
    if (a && a !== document.body && el.contains(a)) return
    if (a && a !== document.body) { inside.current = false; return }
    ;(el.querySelector<HTMLElement>('button:not([disabled]), select') ?? el).focus()
  }, [state])
  const handlers = {
    onPointerDownCapture: () => { inside.current = true },
    onKeyDownCapture: () => { inside.current = true },
    onBlurCapture: (e: React.FocusEvent) => { if (e.relatedTarget && !ref.current?.contains(e.relatedTarget as Node)) inside.current = false },
  }
  return { ref, handlers }
}

/** How many of a confirm's items it names before "+N more". */
export const CONFIRM_LIST = 3

/** A confirm's items, named: the first `CONFIRM_LIST` (a folder with its trailing `/`), then "+N more" with the
 *  rest's folders and files counted separately. */
export function ConfirmItems({ t, scheme }: { t: Targets; scheme: string }) {
  const head = t.items.slice(0, CONFIRM_LIST)
  const rest = t.items.slice(CONFIRM_LIST)
  const folders = rest.filter(i => i.kind === 'prefix').length
  return (
    <span className="act-items">
      {head.map(i => { const p = i.key.slice(scheme.length); return <code key={i.key} title={p}>{elideMid(p, 60, 28)}</code> })}
      {rest.length > 0 && <span className="act-more">+{rest.length.toLocaleString('en-US')} more ({targetsText({ folders, files: rest.length - folders })})</span>}
    </span>
  )
}

const verbOf = (r: ActReq) => (r.kind === 'stage' ? 'stage' : r.owner === null ? 'unassign' : `assign → ${r.who ?? 'you'}`)

/** A state's look, inside one live region (`role="status"`): empty while idle. */
export function ActStatus({ state, confirmT, act, fmtBytes, list }: {
  state: ActState
  confirmT: Targets | null
  act: Pick<ActController, 'confirm' | 'cancel' | 'undo' | 'dismiss' | 'retry'>
  fmtBytes: (b: number) => string
  /** Name the confirm's items (`ConfirmItems`, keys below this scheme): a row's, which has no review list. */
  list?: { scheme: string }
}) {
  const x = <button type="button" className="act x" aria-label="dismiss" onClick={act.dismiss}>×</button>
  const body = (() => {
    switch (state.s) {
      case 'idle': return null
      case 'resolving': return <span className="act-st busy"><span className="spin" aria-hidden="true" />listing the matches…</span>
      case 'confirm': {
        const t = confirmT!
        const n = chunks(t.items, state.req.kind === 'stage' ? STAGE_CHUNK : ASSIGN_CHUNK).length
        const bk = state.req.kind === 'assign' ? bucketsIn(t) : []
        return (
          <span className="act-confirm">
            {verbOf(state.req)} <b>{targetsText(t)}</b> ({fmtBytes(t.b)}, {plural(t.o, 'object')}){n > 1 ? `, sent in ${n} batches` : ''}?
            {bk.length > 0 && <span className="act-note"> {bk.length === 1 ? <>Includes the whole bucket <code>{bk[0]}</code></> : <>Includes {bk.length} whole buckets</>}: this overrides every assignment under it.</span>}
            {list && <ConfirmItems t={t} scheme={list.scheme} />}
            {caveats(t, state.req.kind).map(c => <span key={c} className="act-note"> {c}</span>)}
            <button type="button" className="act go" disabled={!t.items.length} onClick={() => void act.confirm()}>confirm</button>
            <button type="button" className="act" onClick={act.cancel}>cancel</button>
          </span>
        )
      }
      case 'sending': {
        const ing = state.req.kind === 'stage' ? 'staging' : state.req.owner === null ? 'unassigning' : 'assigning'
        if (state.batches <= 1) return <span className="act-st busy"><span className="spin" aria-hidden="true" />{ing} {targetsText(state.t)}…</span>
        return (
          <span className="act-st busy">
            <span className="act-bar" role="progressbar" aria-label={`${ing}: batches sent`} aria-valuemin={0} aria-valuemax={state.batches} aria-valuenow={state.batch}>
              <i style={{ width: `${Math.round((100 * state.batch) / state.batches)}%` }} />
            </span>
            {ing} {state.batch}/{state.batches} batches
          </span>
        )
      }
      case 'done': return <>
        <span className="act-st ok">✓ {state.text}</span>
        {state.req.kind === 'stage' && <a className="act" href="/staged">review staged</a>}
        {state.undo && <button type="button" className="act" onClick={() => void act.undo()}>undo</button>}
        {x}
      </>
      case 'undoing': return <span className="act-st busy"><span className="spin" aria-hidden="true" />undoing…</span>
      case 'muted': return <>
        <Tooltip content={<span className="bb-tip">{state.reason}</span>}><span className="act-st muted" tabIndex={0}>{state.reason}</span></Tooltip>
        {x}
      </>
      case 'failed': return <>
        <span className="act-st err">Can’t {state.req.kind === 'stage' ? 'stage' : state.req.owner === null ? 'unassign' : 'assign'}: {state.msg}</span>
        <button type="button" className="act" onClick={() => void act.retry()}>retry</button>
        {x}
      </>
    }
  })()
  return <span className="act-live" role="status" aria-live="polite">{body}</span>
}

/** Where a row's matches come from (`App`: the cached view's slice, else the row's own cover). */
export interface RowSource {
  /** Changes with the filter, scan or scope: each row's state starts over. */
  key: string
  resolve: (row: string) => Promise<Resolved>
  /** Start the fetch on intent: a nop when cached or in flight. */
  prefetch: (row: string) => Promise<unknown>
  /** Why this view's matches can't be acted on (`actBlock`): no row offers an action, nor does the selection. */
  blocked?: string | null
}

export type Warm = 'cold' | 'warming' | 'ready'

/** A filtered row's controls, drawn: trash and assign while idle (`aria-busy` while its matches prefetch), the
 *  state in their place after a click. */
export function RowActsView({ state, confirmT, act, warm, canTrash, assigning, fmtBytes, scheme, onIntent, focus }: {
  state: ActState
  confirmT: Targets | null
  act: Pick<ActController, 'start' | 'confirm' | 'cancel' | 'undo' | 'dismiss' | 'retry'>
  warm: Warm
  canTrash: boolean
  assigning: boolean
  fmtBytes: (b: number) => string
  scheme: string
  onIntent?: () => void
  focus?: ReturnType<typeof useKeepFocus>
}) {
  return (
    <span className={`mact${warm === 'warming' ? ' warming' : ''}`} aria-busy={warm === 'warming' || undefined} tabIndex={-1}
      ref={focus?.ref} {...focus?.handlers} onMouseEnter={onIntent} onFocus={onIntent}>
      {state.s === 'idle' && <>
        {canTrash && (
          <Tooltip placement="right" content="Stage this row’s matches for deletion (not the rest of the row; listed when you click) — an admin approves and dispatches from /staged">
            <button type="button" className="trash" onClick={() => void act.start({ kind: 'stage' })} aria-label="trash"><FaRegTrashCan /></button>
          </Tooltip>
        )}
        {assigning && <AssignSelect compact onPick={(owner, who) => void act.start({ kind: 'assign', owner, who })} />}
      </>}
      <ActStatus state={state} confirmT={confirmT} act={act} fmtBytes={fmtBytes} list={{ scheme }} />
    </span>
  )
}

/** A filtered row's trash + assign over its matches (`src.resolve(path)`), fetched ahead on intent: the row's
 *  hover (`intent`, after `ROW_DWELL_MS`), or hover / focus of the controls themselves. More than one item
 *  asks first, listing them (`ROW_CONFIRM_OVER`). */
export function RowActs({ path, src, intent, scheme, canTrash, assigning, fmtBytes }: {
  path: string
  src: RowSource
  intent: boolean
  scheme: string
  canTrash: boolean
  assigning: boolean
  fmtBytes: (b: number) => string
}) {
  const send = useActDeps()
  const act = useMatchAct({ ...send, scheme, confirmOver: ROW_CONFIRM_OVER, resolve: () => src.resolve(path) }, src.key)
  const [warm, setWarm] = useState<Warm>('cold')
  const warmRef = useRef<Warm>('cold')
  useEffect(() => { warmRef.current = 'cold'; setWarm('cold') }, [src.key])
  const pre = useCallback(() => {
    if (warmRef.current !== 'cold') return
    warmRef.current = 'warming'
    setWarm('warming')
    const key = src.key
    src.prefetch(path).then(() => { if (src.key === key) { warmRef.current = 'ready'; setWarm('ready') } }, () => { warmRef.current = 'cold'; setWarm('cold') })
  }, [src, path])
  useEffect(() => {
    if (!intent) return
    const t = setTimeout(pre, ROW_DWELL_MS)
    return () => clearTimeout(t)
  }, [intent, pre])
  const focus = useKeepFocus(act.state.s)
  return <RowActsView state={act.state} confirmT={act.confirmT} act={act} warm={warm} canTrash={canTrash} assigning={assigning} fmtBytes={fmtBytes} scheme={scheme} onIntent={pre} focus={focus} />
}
