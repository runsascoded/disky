// One action on a filter's matches (a table row's trash / assign, the selection bar's, the bulk bar's), as a
// small state machine with no DOM: offered at once (no "list" step first), the matches resolved on the
// click (the hover usually started that fetch), then confirmed if large, sent in batches with progress, and
// accepted — or, when they can't be acted on here (over the cap, a refused query), a muted note in place
// of the control. Only a 5xx or a network failure is red.
import type { CoverItem } from '../functions/_lib/cover'
import { ASSIGN_CHUNK, assignInBatches, chunks, isBucketPrefix, isRefusal, type OwnerPost, ownerPost, STAGE_CHUNK, stageMany, unstageMany } from './batches'
import { actionTargets, type Resolved, type Targets, targetsText } from './coverTargets'

export type ActKind = 'assign' | 'stage'

/** What the viewer asked for: assign (to `owner`, `'@me'` by default; null unassigns) or stage. */
export interface ActReq {
  kind: ActKind
  owner?: string | null
  /** The assignee as the viewer named them (`you`, `nobody`, a name): the accepted state says it. */
  who?: string
  /** The owner action's memo / the staging batch's note. */
  memo?: string
}

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

/** What an action says about the items it leaves out, in plain words (one sentence per caveat). */
export function caveats(t: { buckets: number; unknown: number }, action: ActKind): string[] {
  const out: string[] = []
  if (action === 'stage' && t.buckets) out.push(`${plural(t.buckets, 'whole bucket')} can’t be staged; ${t.buckets === 1 ? 'it is' : 'they are'} left out.`)
  if (t.unknown) out.push(`${plural(t.unknown, 'match', 'matches')} couldn’t be checked (file or folder?) and ${t.unknown === 1 ? 'is' : 'are'} left out; open ${t.unknown === 1 ? 'its' : 'their'} folder to act on ${t.unknown === 1 ? 'it' : 'them'}.`)
  return out
}

/** Past this many items an action asks first, by default (the bulk bar always asks: it has the review list; a
 *  table row asks past one, `ROW_CONFIRM_OVER`). */
export const CONFIRM_OVER = 50

/** What a filtered view's answer is, for whether its matches can be acted on: refused (`term-too-common`, …), a
 *  rollup (a heavy literal's per-child totals) or a catalog answer (`bucketsOnly`: per-bucket totals), or an
 *  approximate read — or one with no matches at all (`none`). */
export interface ActView { refused?: boolean; rollup?: boolean; bucketsOnly?: boolean | 'scoped'; approximate?: boolean; dirsOnly?: boolean; none?: boolean }

/** Why the matches can't be acted on in this view — the bulk bar shows it muted (its tooltip) in place of the
 *  actions, and no row offers any — or null: they can. None of these views lists its matches: a refusal has none,
 *  a rollup or catalog answer has totals, an approximate read may miss some; and a view with no matches has
 *  nothing to act on (last: an approximate or dir-only read's reason says more about why none showed). */
export function actBlock(v: ActView): string | null {
  if (v.refused) return 'This view was refused, so it has no matches to act on. Narrow the term, or drill to where it is answered.'
  if (v.bucketsOnly) return 'Only per-bucket totals are known for this term here, not its matches. Narrow the term to act on them.'
  if (v.rollup) return 'This term is too common to list here: the view shows per-folder totals, not its matches. Narrow the term, or drill in.'
  if (v.dirsOnly) return 'This scan lists folders only, so its file matches aren’t known here. Pick a newer scan to act on the matches.'
  if (v.approximate) return 'These matches are approximate (read without the search index), so some may be missing. Narrow the term, or drill in.'
  if (v.none) return NO_MATCHES_REASON
  return null
}

/** Whether a filtered view's (drilled) root holds no matches at all — no bytes, no objects, no children; not
 *  yet loaded (null) is not "none". */
export const noMatches = (root: { b: number; o: number; c?: unknown[] } | null | undefined): boolean =>
  !!root && root.b <= 0 && !root.o && !root.c?.length

/** `actBlock`'s reason for a view with no matches. */
export const NO_MATCHES_REASON = 'Nothing matches this term here, so there is nothing to act on. Widen the term, or drill out.'

/** The muted label the bulk bar shows in place of its actions (`actBlock`'s reason in its tooltip). */
export const ACT_BLOCKED_LABEL = 'no actions on these matches'
/** The label when there are no matches at all. */
export const NO_MATCHES_LABEL = 'no matches to act on'
export const actBlockedLabel = (reason: string) => (reason === NO_MATCHES_REASON ? NO_MATCHES_LABEL : ACT_BLOCKED_LABEL)

/** A table row's action asks before sending more than one item: its label names one path at most, so a click
 *  must never send more than that without listing what it would send. */
export const ROW_CONFIRM_OVER = 1

export type ActState =
  | { s: 'idle' }
  /** Listing the matches (`/api/filter-cover`, or the cached slice): a spinner. */
  | { s: 'resolving'; req: ActReq }
  /** Large (or a whole bucket's assignment): what it will send, and a confirm. */
  | { s: 'confirm'; req: ActReq; got: Resolved }
  /** Sending: `batch` of `batches` POSTs done (a bar when there are several, else a spinner). */
  | { s: 'sending'; req: ActReq; t: Targets; batch: number; batches: number }
  /** Accepted: what landed, and how to take it back (a staging that absorbed nothing). */
  | { s: 'done'; req: ActReq; text: string; undo?: { plan_id: number; keys: string[] } }
  | { s: 'undoing'; req: ActReq }
  /** Not acted on, by design (over the cap, not all listable, a refusal): the reason, muted. */
  | { s: 'muted'; reason: string }
  /** A 5xx or the network: red, with a retry. */
  | { s: 'failed'; req: ActReq; msg: string }

export interface ActDeps {
  /** The matches the action takes (`rowCover`, the view's cover, …). */
  resolve: () => Promise<Resolved>
  /** The items kept from what resolved (the bulk bar's unticked ones dropped). Default: all. */
  pick?: (got: Resolved) => CoverItem[]
  scheme: string
  /** One `/api/actions` POST (≤ `ASSIGN_CHUNK` actions). */
  assign: (actions: OwnerPost[]) => Promise<unknown>
  /** The plan API (`plans.ts` `call`): stage, merge, unstage. */
  call: <T>(url: string, method: string, body: unknown) => Promise<T>
  /** The scan a staging is "as of". */
  asOf?: () => string | null | undefined
  /** Ask before every send, not just large ones. */
  alwaysConfirm?: boolean
  /** Ask past this many items (default `CONFIRM_OVER`). */
  confirmOver?: number
  /** After anything landed (refresh what shows it). */
  landed?: (kind: ActKind) => void
}

/** The targets an action takes from what resolved. */
export const targetsOf = (got: Resolved, req: ActReq, d: Pick<ActDeps, 'pick' | 'scheme'>): Targets =>
  actionTargets(d.pick ? d.pick(got) : got.items, d.scheme, req.kind)

/** Whole buckets among an assignment's targets: the newest action on an ancestor overrides everything under
 *  it, so one is never assigned without asking. */
export const bucketsIn = (t: Targets): string[] => t.items.filter(i => i.kind === 'prefix' && isBucketPrefix(i.key)).map(i => i.key)

/** Why nothing would be sent, in words (every match unchecked, or only whole buckets to stage). */
export function nothingReason(t: Targets, kind: ActKind): string {
  if (t.unknown && !t.buckets) return 'These matches couldn’t be checked (file or folder?); open the folder to act on them.'
  if (t.buckets && kind === 'stage') return 'Whole buckets can’t be staged for deletion; open the bucket to act on its matches.'
  return 'Nothing here to act on.'
}

/** What follows a resolve: muted (not all listed, or nothing to send), a confirm, or straight to sending. */
export function afterResolve(got: Resolved, req: ActReq, d: Pick<ActDeps, 'pick' | 'scheme' | 'alwaysConfirm' | 'confirmOver'>): ActState | { s: 'go'; t: Targets } {
  if (!got.complete) return { s: 'muted', reason: got.reason ?? 'These matches can’t all be listed here; open a folder below to act on its matches.' }
  const t = targetsOf(got, req, d)
  if (!t.items.length) return { s: 'muted', reason: nothingReason(t, req.kind) }
  if (d.alwaysConfirm || t.items.length > (d.confirmOver ?? CONFIRM_OVER) || (req.kind === 'assign' && bucketsIn(t).length)) return { s: 'confirm', req, got }
  return { s: 'go', t }
}

/** A failure's state: a refusal (4xx) muted, anything else red. */
export const failState = (e: unknown, req: ActReq): ActState =>
  isRefusal(e) ? { s: 'muted', reason: (e as Error).message } : { s: 'failed', req, msg: e instanceof Error ? e.message : String(e) }

const chunkOf = (k: ActKind) => (k === 'stage' ? STAGE_CHUNK : ASSIGN_CHUNK)

/** The controller over a state cell (`get` / `set`): `start` on a click, `confirm` / `cancel` the large-set
 *  question, `undo` an accepted staging, `retry` a failure, `dismiss` back to idle. Each step checks it is
 *  still current (a cancel or dismiss mid-resolve drops the late answer). */
export function actController(deps: () => ActDeps, set: (s: ActState) => void, get: () => ActState) {
  const send = async (req: ActReq, t: Targets) => {
    const d = deps()
    const batches = chunks(t.items, chunkOf(req.kind)).length
    let cur: ActState = { s: 'sending', req, t, batch: 0, batches }
    set(cur)
    const progress = (done: number) => { cur = { ...cur, batch: Math.ceil(done / chunkOf(req.kind)) } as ActState; set(cur) }
    try {
      if (req.kind === 'assign') {
        await assignInBatches(t.items, i => ownerPost(i, req.owner === undefined ? '@me' : req.owner, req.memo), d.assign, progress)
        set({ s: 'done', req, text: `${req.owner === null ? 'unassigned' : 'assigned'} ${targetsText(t)}${req.owner === null ? '' : ` → ${req.who ?? 'you'}`}` })
      } else {
        const r = await stageMany(t.items, req.memo, d.asOf?.(), d.call, progress)
        const files = r.staged_objects?.length ?? 0
        const text = r.staged.length ? `staged ${targetsText({ folders: r.staged.length - files, files })}` : 'already staged'
        set({ s: 'done', req, text, ...(r.staged.length && !r.absorbed.length && r.plan_id != null ? { undo: { plan_id: r.plan_id, keys: r.staged } } : {}) })
      }
      d.landed?.(req.kind)
    } catch (e) {
      set(failState(e, req))
    }
  }
  const start = async (req: ActReq) => {
    const s0 = get().s
    if (s0 === 'resolving' || s0 === 'sending' || s0 === 'undoing') return
    set({ s: 'resolving', req })
    let got: Resolved
    try {
      got = await deps().resolve()
    } catch (e) {
      if (get().s === 'resolving') set(failState(e, req))
      return
    }
    const cur = get()
    if (cur.s !== 'resolving' || cur.req !== req) return
    const next = afterResolve(got, req, deps())
    if (next.s === 'go') await send(req, next.t)
    else set(next)
  }
  const confirm = async () => {
    const cur = get()
    if (cur.s !== 'confirm') return
    const t = targetsOf(cur.got, cur.req, deps())
    if (!t.items.length) { set({ s: 'muted', reason: nothingReason(t, cur.req.kind) }); return }
    await send(cur.req, t)
  }
  const undo = async () => {
    const cur = get()
    if (cur.s !== 'done' || !cur.undo) return
    const d = deps()
    set({ s: 'undoing', req: cur.req })
    try {
      const n = await unstageMany(cur.undo.plan_id, cur.undo.keys, d.call)
      set({ s: 'done', req: cur.req, text: `unstaged ${n.toLocaleString('en-US')} ${n === 1 ? 'item' : 'items'}` })
      d.landed?.(cur.req.kind)
    } catch (e) {
      set(failState(e, cur.req))
    }
  }
  const cancel = () => { if (get().s === 'confirm' || get().s === 'resolving') set({ s: 'idle' }) }
  const dismiss = () => { const s = get().s; if (s === 'done' || s === 'muted' || s === 'failed') set({ s: 'idle' }) }
  const retry = () => { const cur = get(); if (cur.s === 'failed') { set({ s: 'idle' }); return start(cur.req) } }
  return { start, confirm, cancel, undo, dismiss, retry }
}
export type ActController = ReturnType<typeof actController>
