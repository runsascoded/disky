/**
 * The staged-deletion review loop in Slack (specs/done/staged-slack.md): one thread
 * per staged plan in the deployment's admin channel (`SLACK_ADMIN_CHANNEL`,
 * the only place a channel comes from). The parent message is re-rendered on
 * every event (counts, size, the latest dry-run, whether it still matches the
 * plan, the action buttons); each event is a reply. A stager's stages within
 * `COALESCE_S` of their reply's last update edit that one reply in place. A ✅
 * on the parent (or an emptied queue) moves the plan to a new thread on the
 * next event. Automatic posts name people in plain text, never by mention.
 * The pure parts (`renderParent`, `stageReply`, the event texts) are what the
 * unit tests pin; the rest is Slack + D1 I/O,
 * best-effort — a Slack failure never fails the gesture that caused it.
 */
import type { D1Database } from '@cloudflare/workers-types'
import type { RunOutcome } from '../../src/runs.js'
import { type FinishedRun, openPlanId, planDigest, planItemLines, planRuns, type RunRow, scanDeletions, unstageDeleted } from './plans.js'
import { withoutDeleted } from '../../src/stagedScan.js'
import { slackApi, slackPeople, type SlackPerson, slackReady, type SlackEnv, type SlackResult } from './slack.js'
import type { Env } from './auth.js'
import { runHasNoUndo, runPromisedNothing, undoneText, type UndoUnavailable, undoUnavailableText } from '../../src/softDelete.js'
import { runUndoNow } from './softDeleteNow.js'
import { pathScans, storeReady } from './index.js'
import { MAX_PREFIXES, type PrefixStat, prefixesAt } from './prefixes.js'
import { expDay, IMAGE_TTL_DAYS, imagePath, ogKey } from './og/sign.js'
import { imageParams } from './og/cred.js'
import { serverToken } from './og/tokens.js'
import { canonId, loadRegistry } from './identity.js'
import type { Registry } from '../../src/identityRegistry.js'
import { BATCH_GAP_S } from './batchGroups.js'
import { Memo, reqOf } from './shared.js'
import { lastRunOutcome } from './sweepOutcome.js'

export type { RunRow }

export const fmtBytes = (n: number): string => {
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${i ? v.toFixed(1) : v} ${u[i]}`
}
const fmtN = (n: number): string => n.toLocaleString('en-US')
/** A count at a glance: `33.9M`, exact below a million. */
const fmtCount = (n: number): string => n < 1e6 ? fmtN(n) : n < 1e9 ? `${(n / 1e6).toFixed(1)}M` : `${(n / 1e9).toFixed(1)}B`
const utc = (ts: number): string => new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + 'Z'
const plural = (n: number, one: string, many = `${one}s`): string => `${fmtN(n)} ${n === 1 ? one : many}`

/** Lowercased email → how automatic posts name them (their Slack name; absent
 *  = the email's local part). Never a mention: these posts notify no one. */
export type Names = Record<string, string>

export interface ParentView {
  planId: number
  siteUrl: string
  items: number
  batches: number
  stagers: string[]
  runs: RunRow[]
  digest: string
  /** Offer Dry-run and the link to delete (the deployment's executor is wired). */
  actions: boolean
  closed: boolean
  names?: Names
  /** The staged set's size at the latest scan. */
  size?: Sized
  /** The plan's share card (a signed, full-tier `/og/staged.png` URL). */
  image?: string
  /** What is still queued: the items no real run has deleted (the title and
   *  "staged by"). */
  queued?: { items: number; batches: number; stagers: string[] }
  /** Unix seconds: whether a real run is still undoable (default: now). */
  now?: number
  /** What the last real run's run dir says of its totals (`lastRunOutcome`); absent or null: nothing to add. */
  lastOutcome?: RunOutcome | null
  /** The last real run's recorded undo can't be done now: soft delete is off on a bucket it deleted from (`runUndoNow`). Absent
   *  or null: not known off. */
  undoUnavailable?: UndoUnavailable | null
  /** Nothing says whether its buckets' soft delete is still on (no reading, or none of a bucket of it): the deadline is
   *  stated as depending on it. Absent: every bucket of it is on as of the reading, or an executor that needs none. */
  undoUnseen?: boolean
}

/** A staged set at one scan: totals and its largest owners (by display name). */
export interface Sized {
  scan: string
  b: number
  o: number
  /** Prefixes with nothing left at `scan` (`emptyAt`): left out of every
   *  count, and named apart. */
  empty: number
  owners: { label: string; b: number }[]
}

const who = (email: string, names?: Names): string => names?.[email.toLowerCase()] ?? email.replace(/@.*$/, '')

/** A staged prefix with nothing under it at the scan (no bytes, no objects;
 *  absent from the lookup): `/staged`'s "Empty at scan". Not counted as staged. */
export const emptyAt = (st: PrefixStat | undefined): boolean => !st || (!st.b && !st.o)

/** The queue's total, as the parent's first line states it: `4,886 prefixes · 51.0 TiB · 179.3M objects* at scan 2026-10-02`
 *  (unsized: the prefixes alone). */
export const totalLine = (items: number, z?: Sized): string => {
  const prefixes = plural(items, 'prefix', 'prefixes')
  return z ? `*${prefixes} · ${fmtBytes(z.b)} · ${fmtCount(z.o)} objects* at scan ${z.scan}` : `*${prefixes}*`
}

/** The lines under the total: the largest owners only when they hold most of it (a few names beside a mostly unowned
 *  total say nothing), and how many staged prefixes were empty at the scan. */
export function sizeNotes(z: Sized): string[] {
  const named = z.owners.reduce((n, o) => n + o.b, 0)
  return [
    ...(z.owners.length && 2 * named >= z.b ? [`owners: ${z.owners.map(o => `${o.label} ${fmtBytes(o.b)}`).join(', ')}`] : []),
    ...(z.empty ? [`+${plural(z.empty, 'staged prefix', 'staged prefixes')} empty at scan, not counted`] : []),
  ]
}

/** The plan's newest real run. */
const lastRealRun = (runs: readonly RunRow[]): RunRow | undefined => [...runs].filter(r => r.mode === 'real').sort((a, b) => b.started_ts - a.started_ts)[0]

/** What a real run's event says of undo: that it was undone (nothing is left to qualify), or its window (or, `unavailable`, that
 * soft delete is off now where it deleted; after a partial undo, the rest's), that it has none, or (no record either way) nothing. */
const undoNote = (r: RunRow, unavailable?: UndoUnavailable | null): string => {
  const undone = undoneText(r)
  const since = undone ? `; since ${undone}` : ''
  if (r.undo_state === 'full') return since
  return since + (r.undo_deadline ? unavailable ? `; ${undoUnavailableText(unavailable)}` : `${since ? ', the rest' : ';'} undoable until ${utc(r.undo_deadline)} (www)`
    : runPromisedNothing(r) && r.mode === 'real' ? '; no undo promised (no soft-delete minimum)'
    : runHasNoUndo(r) ? '; no undo (soft delete off)' : '')
}

/** …and the parent's line for its last real run, inside the window it recorded (`deadline`). */
const undoClause = (r: RunRow, deadline: number, v: Pick<ParentView, 'undoUnavailable' | 'undoUnseen'>): string => {
  const undone = undoneText(r)
  const since = undone ? `, since ${undone}` : ''
  if (r.undo_state === 'full') return since
  return since + (v.undoUnavailable ? `; ${undoUnavailableText(v.undoUnavailable)}`
    : `${since ? '; the rest' : ','} undoable until ${utc(deadline)}${v.undoUnseen ? ' if the buckets\' soft delete is still on (this site can\'t see it)' : ''}`)
}

/** The thread's parent message: `{ text, blocks }` for chat.postMessage / chat.update. */
export function renderParent(v: ParentView): { text: string; blocks: unknown[] } {
  const q = v.queued ?? { items: v.items, batches: v.batches, stagers: v.stagers }
  const title = v.closed ? `*Staged plan #${v.planId}* (closed)` : `*Staged for deletion* · plan #${v.planId}`
  const by = q.stagers.length ? `staged by ${q.stagers.map(e => who(e, v.names)).join(', ')}` : 'nothing staged'
  const head = [`${title}: ${totalLine(q.items, v.size)}`, ...(v.size ? sizeNotes(v.size) : []), by]
  const now = v.now ?? Math.floor(Date.now() / 1000)
  // Only what bears on the current set: a dry-run of a set since changed, or a real run past its undo window, is history (the thread and www keep it).
  const latestDry = [...v.runs].filter(r => r.mode === 'dry').sort((a, b) => b.started_ts - a.started_ts)[0]
  const dryLine = !latestDry ? null
    : latestDry.finished_ts == null ? `Dry-run running (${who(latestDry.actor, v.names)}, scan ${latestDry.scan}).`
    : latestDry.plan_digest === '' ? `Latest dry-run (\`${latestDry.run_id}\`) ended without a result.`
    : latestDry.plan_digest === v.digest ? `Latest dry-run would delete *${fmtBytes(latestDry.deleted_bytes)}* / ${fmtN(latestDry.deleted_objects)} objects (scan ${latestDry.scan}) — matches the current plan.`
    : null
  const lastReal = lastRealRun(v.runs)
  const realLine = !lastReal ? null
    : lastReal.finished_ts == null ? `Real run in progress (${who(lastReal.actor, v.names)}).`
    : lastReal.undo_deadline != null && lastReal.undo_deadline > now ? `Last real run ${v.lastOutcome?.unread ? 'recorded' : v.lastOutcome?.attempted ? 'attempted' : 'deleted'} ${v.lastOutcome?.totals === 'lower-bound' ? 'at least ' : ''}${fmtBytes(lastReal.deleted_bytes)} / ${fmtN(lastReal.deleted_objects)} objects${undoClause(lastReal, lastReal.undo_deadline, v)}.${skipsLine(v.lastOutcome ?? undefined)}`
    : null
  const buttons: unknown[] = [
    { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'Open in www' }, url: `${v.siteUrl}/staged` },
  ]
  if (v.actions && !v.closed && v.items) {
    buttons.push({ type: 'button', action_id: 'staged_dry', value: String(v.planId), text: { type: 'plain_text', text: 'Dry-run' } })
    // A link: a real delete needs a site session, and no dry run (`plans.realRun`). Its own `action_id`: Slack refuses two alike in a block.
    buttons.push({ type: 'button', action_id: 'staged_delete_link', text: { type: 'plain_text', text: 'Delete for real…' }, url: `${v.siteUrl}/staged` })
  }
  const status = [dryLine, realLine].filter(Boolean).join('\n')
  const blocks = [
    { type: 'section', text: { type: 'mrkdwn', text: head.join('\n') } },
    // Slack refuses a section with no text.
    ...(status ? [{ type: 'section', text: { type: 'mrkdwn', text: status } }] : []),
    { type: 'actions', elements: buttons },
    ...(v.image && v.items ? [{ type: 'image', image_url: v.image, alt_text: `Plan #${v.planId}: the staged prefixes as a treemap, coloured by owner` }] : []),
  ]
  return { text: `Staged for deletion: plan #${v.planId}, ${plural(q.items, 'prefix', 'prefixes')}`, blocks }
}

type ParentMessage = ReturnType<typeof renderParent>

/** Post (`ts` null) or update the parent, what it says landing even when its card can't: Slack fetches an image block's
 *  URL during the call and refuses the whole message (`invalid_blocks`) when that fails or outlasts its wait, as a new
 *  card's cold render over a large plan can. Then the text goes without the card, the card is rendered (its colo copy is
 *  what Slack's next fetch reads), and the card is put back. */
export async function sendParent(env: SlackEnv, channel: string, ts: string | null, parent: ParentMessage): Promise<SlackResult> {
  const send = (at: string | null, m: ParentMessage): Promise<SlackResult> => at
    ? slackApi(env, 'chat.update', { channel, ts: at, ...m })
    : slackApi(env, 'chat.postMessage', { channel, ...m, ...PLAN_SENDER, unfurl_links: false })
  const got = await send(ts, parent)
  const image = (parent.blocks as { type: string; image_url?: string }[]).find(b => b.type === 'image')?.image_url
  if (got.ok || got.error !== 'invalid_blocks' || !image) return got
  const bare = await send(ts, { ...parent, blocks: (parent.blocks as { type: string }[]).filter(b => b.type !== 'image') })
  const at = ts ?? bare.ts
  if (!bare.ok || !at) return bare
  const rendered = await fetch(image).then(r => r.arrayBuffer().then(() => r.ok)).catch(() => false)
  if (rendered) await send(at, parent)
  return bare
}

/** The old parent once its plan moved to a new thread (`link`: the new parent). */
export function closedParent(planId: number, siteUrl: string, link: string | null): { text: string; blocks: unknown[] } {
  const to = link ? `<${link}|a new thread>` : 'a new thread'
  return {
    text: `Staged plan #${planId}: continued in a new thread`,
    blocks: [
      { type: 'section', text: { type: 'mrkdwn', text: `*Staged plan #${planId}* · this thread is done; continued in ${to}.` } },
      { type: 'actions', elements: [{ type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'Open in www' }, url: `${siteUrl}/staged` }] },
    ],
  }
}

const unscheme = (p: string): string => p.replace(/^[a-z0-9]+:\/\//, '')
/** `p`'s parent folder (`gs://b/x/y/` → `gs://b/x/`); a bucket root is its own. */
const parentOf = (p: string): string => {
  const s = p.replace(/\/$/, '')
  const i = s.lastIndexOf('/')
  return i > s.indexOf('://') + 2 ? s.slice(0, i + 1) : p
}
/** A path for a phone-width line: past `max` chars, the bucket, `…`, and the
 *  last two folders. */
export function shortPath(p: string, max = 48): string {
  const s = unscheme(p)
  if (s.length <= max) return s
  const segs = s.replace(/\/$/, '').split('/')
  return segs.length > 3 ? `${segs[0]}/…/${segs.slice(-2).join('/')}/` : s
}

/** The largest few folders of a staged set, as one line: the prefixes
 *  themselves when there are at most `n`, else grouped by parent folder (a
 *  folder holding one staged prefix shows that prefix). By bytes at the scan,
 *  or, unsized (`stats` null), by how many prefixes. */
export function topFolders(prefixes: readonly string[], stats: Record<string, PrefixStat> | null, n = 3): string {
  const groups = new Map<string, { k: number; b: number; only: string }>()
  for (const p of prefixes) {
    const key = prefixes.length <= n ? p : parentOf(p)
    const g = groups.get(key)
    const b = stats?.[p]?.b ?? 0
    if (g) { g.k++; g.b += b } else groups.set(key, { k: 1, b, only: p })
  }
  const sorted = [...groups].sort(([ka, a], [kb, b]) => (stats ? b.b - a.b : 0) || b.k - a.k || (ka < kb ? -1 : ka > kb ? 1 : 0))
  const top = sorted.slice(0, n).map(([key, g]) => ({ path: unscheme(g.k === 1 ? g.only : key), g }))
  // The folder every shown path shares, named once; each path then reads
  // from there, so what tells them apart (usually a run name) survives.
  const shared = top.length > 1 ? commonFolder(top.map(t => t.path)) : ''
  const root = shared.split('/').filter(Boolean).length > 1 ? shared : ''  // just the bucket: not worth a line of its own
  const shown = top.map(({ path, g }) => {
    const name = `\`${root ? relPath(path.slice(root.length)) : shortPath(path)}\``
    const count = g.k > 1 ? ` (${fmtN(g.k)})` : ''
    return `${name}${count}${stats ? ` ${fmtBytes(g.b)}` : ''}`
  })
  const more = sorted.length - shown.length
  return (root ? `in \`${shortPath(root)}\`: ` : '') + shown.join(', ') + (more > 0 ? `, +${fmtN(more)} more` : '')
}

/** The deepest folder (ending in `/`) that every path lies under; '' if none. */
export function commonFolder(paths: readonly string[]): string {
  if (!paths.length) return ''
  let c = paths[0]
  for (const p of paths.slice(1)) { let i = 0; while (i < c.length && i < p.length && c[i] === p[i]) i++; c = c.slice(0, i) }
  const cut = c.lastIndexOf('/')
  const root = cut < 0 ? '' : c.slice(0, cut + 1)
  // A shown path equal to the root would read as empty: back off one level.
  return paths.some(p => p === root) ? root.slice(0, root.slice(0, -1).lastIndexOf('/') + 1) : root
}

/** A path relative to a shared folder, for a phone-width line: past `max`
 *  chars, its first folder (the one that tells siblings apart; a long one
 *  elided in its middle), `…`, and its last. */
export function relPath(rel: string, max = 52): string {
  if (rel.length <= max) return rel
  const segs = rel.replace(/\/$/, '').split('/')
  const first = segs[0].length > 40 ? `${segs[0].slice(0, 24)}…${segs[0].slice(-14)}` : segs[0]
  return segs.length === 1 ? `${first}/` : segs.length === 2 ? `${first}/${segs[1]}/` : `${first}/…/${segs[segs.length - 1]}/`
}

/** A stage reply: one stager's batches (one, or several coalesced). */
export interface ReplyView {
  planId: number
  /** The `stage_replies` row (its reject button rejects every batch it covers);
   *  null for a pre-coalescing single-batch reply, whose button names `batchId`. */
  replyId: number | null
  batchId?: number
  /** The stager, as named (`who`). */
  by: string
  /** Every batch it covers, live or not (named only once nothing is left). */
  batches: number
  /** What its batches still have staged. */
  prefixes: string[]
  /** Each prefix's batch: the counts shown are of the batches still holding a
   *  path (a batch absorbed or unstaged entirely drops out), as the parent's are. */
  batchOf: Record<string, number>
  /** Bytes / objects by prefix at the latest scan; null = unsized. */
  stats: Record<string, PrefixStat> | null
  /** The latest batch note, if any. */
  note: string | null
  siteUrl: string
}

/** A stage reply, compact enough for a phone: who staged how many paths in how
 *  many batches and their size, the largest folders, the latest note. */
export function stageReply(v: ReplyView): { text: string; blocks: unknown[] } {
  const n = v.prefixes.length
  // Sized, the paths empty at the scan are left out of the counts and named apart.
  const held = v.stats ? v.prefixes.filter(p => !emptyAt(v.stats![p])) : v.prefixes
  const empty = n - held.length
  let b = 0, o = 0
  for (const p of held) { b += v.stats?.[p]?.b ?? 0; o += v.stats?.[p]?.o ?? 0 }
  const batchesOf = (ps: readonly string[]) => new Set(ps.map(p => v.batchOf[p])).size
  // The batches holding what is shown (the title), and what a reject unstages (the button).
  const shown = batchesOf(held.length ? held : v.prefixes)
  const live = batchesOf(v.prefixes)
  const inBatches = shown > 1 ? ` in ${plural(shown, 'batch', 'batches')}` : ''
  const emptyNote = empty ? ` (+${fmtN(empty)} empty at scan)` : ''
  const size = v.stats ? ` · *${fmtBytes(b)}* · ${plural(o, 'object')}${emptyNote}` : ''
  const note = v.note ? `\n> ${(v.note.length > 140 ? `${v.note.slice(0, 139)}…` : v.note).replace(/\s*\n\s*/g, ' ')}` : ''
  const view = { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'View in www' }, url: `${v.siteUrl}/staged` }
  if (!n) {
    return {
      text: `${v.by} staged ${plural(v.batches, 'batch', 'batches')}: nothing left staged`,
      blocks: [
        { type: 'section', text: { type: 'mrkdwn', text: `:wastebasket: *${v.by}* staged ${plural(v.batches, 'batch', 'batches')} · nothing left staged${note}` } },
        { type: 'actions', elements: [view] },
      ],
    }
  }
  const many = live > 1
  const reject = {
    type: 'button', action_id: 'staged_reject', value: v.replyId != null ? `${v.planId}:r${v.replyId}` : `${v.planId}:${v.batchId}`,
    text: { type: 'plain_text', text: many ? `Reject ${fmtN(live)} batches` : 'Reject batch' },
    confirm: {
      title: { type: 'plain_text', text: many ? `Reject these ${fmtN(live)} batches?` : 'Reject this batch?' },
      text: { type: 'mrkdwn', text: `Unstages the ${plural(n, 'path')} ${many ? 'they' : 'it'} staged. Nothing is deleted.` },
      confirm: { type: 'plain_text', text: 'Reject' },
      deny: { type: 'plain_text', text: 'Cancel' },
    },
  }
  if (!held.length) {
    const head = `${v.by} staged ${plural(n, 'path')}${inBatches} · all empty at scan`
    return { text: head, blocks: [{ type: 'section', text: { type: 'mrkdwn', text: `:wastebasket: *${v.by}* staged ${plural(n, 'path')}${inBatches} · all empty at scan${note}` } }, { type: 'actions', elements: [reject, view] }] }
  }
  return {
    text: `${v.by} staged ${plural(held.length, 'path')}${inBatches}${v.stats ? ` · ${fmtBytes(b)}${emptyNote}` : ''}`,
    blocks: [
      { type: 'section', text: { type: 'mrkdwn', text: `:wastebasket: *${v.by}* staged ${plural(held.length, 'path')}${inBatches}${size}\n${topFolders(held, v.stats)}${note}` } },
      { type: 'actions', elements: [reject, view] },
    ],
  }
}

/** A run's page on the site: `/staged?run=<id>` opens and scrolls to its row
 *  (the run id, or its Batch job's id before the executor records it). */
export const runUrl = (siteUrl: string, runId: string): string => `${siteUrl}/staged?run=${encodeURIComponent(runId)}`

/** What a run left alone, as its `/staged` row says it; empty when nothing was. */
function skipsLine(o?: RunOutcome): string {
  if (!o?.skipped) return ''
  const notSwept = o.notSwept.length ? ` *Not swept:* ${o.notSwept.map(b => `\`${b}\``).join(', ')} (no object in them had a generation; their prefixes stay staged).` : ''
  return ` Skipped, no generation: *${fmtBytes(o.skipped.bytes)}* / ${fmtN(o.skipped.objects)} objects left in place.${notSwept}`
}

/** A run's thread reply: the actor by name, the run id linking to its row on
 *  `/staged`. */
export function runEvent(r: RunRow, phase: 'dispatched' | 'finished' | 'failed', o: { via?: string | null; names?: Names; siteUrl?: string; reason?: string; outcome?: RunOutcome; expected?: { bytes: number; objects: number }; undoUnavailable?: UndoUnavailable | null } = {}): string {
  const kind = r.mode === 'dry' ? 'Dry-run' : '*Real deletion*'
  const id = o.siteUrl ? `<${runUrl(o.siteUrl, r.run_id)}|${r.run_id}>` : `\`${r.run_id}\``
  if (phase === 'dispatched') return `${r.mode === 'dry' ? ':test_tube:' : ':rotating_light:'} ${kind} dispatched by ${who(r.actor, o.names)}${o.via ? ` via ${o.via}` : ''} on scan ${r.scan} (${id})${o.expected ? `: expected ${fmtBytes(o.expected.bytes)} / ${fmtN(o.expected.objects)} objects` : ''}`
  if (phase === 'failed' && o.outcome?.totals) {
    const verb = o.outcome.attempted ? 'attempted' : 'deleted'
    const size = `*${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects`
    const until = undoNote(r, o.undoUnavailable)
    const why = o.reason ? ` (\`${o.reason.replace(/`/g, "'").replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}\`)` : ''
    if (o.outcome.totals === 'exact') return `:warning: ${kind} ${id} ended unsuccessfully${why}: ${verb} ${size}${until}.${skipsLine(o.outcome)}`
    const said = o.outcome.totals === 'lower-bound'
      ? `: ${verb} at least ${size}, from its progress files, which can lag what it did`
      : `, and what it wrote couldn't be read, so it may have deleted more than its recorded ${size}`
    return `:warning: ${kind} ${id} ended before recording its end${why}${said}${until}.${runHasNoUndo(r) || (r.undo_deadline && o.undoUnavailable) ? '' : ' Undo restores only what its log names.'}`
  }
  if (phase === 'failed' && o.reason) return `:x: ${kind} ${id} ended without a result: \`${o.reason.replace(/`/g, "'").replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')}\``
  if (phase === 'failed') return `:x: ${kind} ${id} ended without a result (its Batch job stopped before the run summary); check its logs in www.`
  const skips = skipsLine(o.outcome)
  if (r.mode === 'dry') return `:test_tube: Dry-run ${id} finished: would delete *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects (gone since scan: ${fmtN(r.skipped_gone)}, overwritten: ${fmtN(r.skipped_overwritten)}).${skips}`
  if (o.outcome?.unread) return `:warning: Real deletion ${id} finished, totals unread: recorded *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects${undoNote(r, o.undoUnavailable)}.`
  return `${skips ? ':warning:' : ':white_check_mark:'} Real deletion ${id} finished: ${o.outcome?.attempted ? 'attempted' : 'deleted'} *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects${undoNote(r, o.undoUnavailable)}.${skips}`
}

/** No claim that anything is deleted: an XML reply can't prove it, and a run with skips left objects under its prefixes. */
export const QUEUE_EMPTIED = ':checkered_flag: Queue emptied: nothing staged here is left to delete (each prefix had a real run recorded, or was empty at the scan).'

/** Was the queue empty before this stage? Every item other than the ones just
 *  staged (`fresh`) is either deleted by a real run (`deleted`) or holds no
 *  bytes at the latest scan (`stats`: bytes by prefix, absent = none; null =
 *  sizes unknown, so only recorded deletions count). Then the plan's Slack
 *  thread has run its course and the stage starts a new one. */
export function queueWasEmpty(items: readonly string[], fresh: ReadonlySet<string>, deleted: ReadonlySet<string>, stats: Record<string, number> | null): boolean {
  return items.every(p => fresh.has(p) || deleted.has(p) || (stats !== null && !stats[p]))
}

// ── I/O ────────────────────────────────────────────────────────────────────

/** `GCP_SA_KEY` = the deployment can dispatch (either executor), so the
 * parent message offers the dispatch buttons. */
export type NotifyEnv = SlackEnv & { GCP_SA_KEY?: string; EXECUTOR?: string }

/** A stager's stages within this many seconds of their reply's last update
 *  edit that reply instead of posting a new one. */
export const COALESCE_S = BATCH_GAP_S

/** The parent reaction that closes a thread: the next event starts a new one. */
export const DONE_REACTION = 'white_check_mark'

/** A thread reply; `by` (an email) posts it as that person, `text` then naming them (`name`) as the workspace or
 *  the registry does. */
interface Event { text: string | ((name: string) => string); blocks?: unknown[]; sender?: Sender; by?: { email: string; verb: string } }
/** The plan's full-tier card for the parent message, or null when the
 * deployment draws no cards (or has no `og_tokens` table). Slack fetches it
 * once per URL, so the view carries the plan's digest (`v`): a new batch is
 * a new image. Like any full card it's backed by an `og_tokens` row, minted
 * by `slack:staged` and reused while it has a week left, so revoking that row
 * on /admin reverts the thread's card on its next fetch. */
export async function stagedCardUrl(env: NotifyEnv & Env & { OG_CARDS?: string; SESSION_SECRET?: string }, db: D1Database, siteUrl: string, digest: string, now = Math.floor(Date.now() / 1000)): Promise<string | null> {
  // Slack fetches the image itself, so a non-https origin (a local stack) gets none: its URL would fail the post (`invalid_blocks`).
  if (!env.OG_CARDS || !env.SESSION_SECRET || !siteUrl.startsWith('https://')) return null
  const key = await ogKey(env.SESSION_SECRET)
  const tok = await serverToken(db, 'staged', {}, '/staged', 'slack:staged', now, IMAGE_TTL_DAYS).catch(() => null)
  if (!tok) return null
  return siteUrl + await imagePath(key, 'staged', imageParams({}, await stagedImageVersion(env, digest), { t: tok.token }), 'full', Math.min(expDay(now, IMAGE_TTL_DAYS), tok.day))
}

/** The staged card's version: the plan's digest (first 8 hex) and the
 *  latest scan, which the card sizes against — so a new batch or a new scan
 *  is a new image URL, and neither Slack nor the colo cache serves a card
 *  drawn from the plan or scan before. */
export async function stagedImageVersion(env: Env, digest: string): Promise<string> {
  const scans = storeReady(env) ? (await pathScans(env, true).catch(() => null))?.results ?? [] : []
  const scan = scans[scans.length - 1]?.date
  return scan ? `${digest.slice(0, 8)}.${scan}` : digest.slice(0, 8)
}

/** A stage batch, announced (with its stager's other recent batches) in one reply. */
export interface StageArgs { stage: { planId: number; batchId: number; by: string; prefixes: string[]; covered: number; note: string | null; siteUrl: string } }
/** A run's reply, rendered here so it can carry its actor's name (and, for a
 *  dispatch, post as them). */
export interface RunArgs { run: RunRow; phase: 'dispatched' | 'finished' | 'failed'; via?: string | null; reason?: string; outcome?: RunOutcome; expected?: { bytes: number; objects: number } }

/** How automatic posts name `emails`: their Slack names where the workspace
 *  knows them (callers fall back to the local part). */
export async function slackNames(env: SlackEnv, emails: readonly string[]): Promise<Names> {
  const people = await slackPeople(env, emails)
  return Object.fromEntries(Object.entries(people).flatMap(([e, p]) => p.name ? [[e, p.name]] : []))
}

/** Who a message posts as (`username` / `icon_*`; honoured only with the
 *  `chat:write.customize` scope, ignored otherwise). The thread's parent is
 *  the plan itself; an event posts as the person who did it. */
export { slackPeople, type SlackPerson }

export interface Sender { username: string; icon_url?: string; icon_emoji?: string }
/** `email` as the deployment's registry knows them (the site's own name and GitHub avatar), for someone the Slack
 *  workspace doesn't (a sign-in email outside it). */
export function registryPerson(reg: Registry, email: string): SlackPerson | undefined {
  const rec = reg[canonId(email, reg)]
  return rec ? { name: rec.name, image: rec.github ? `https://avatars.githubusercontent.com/${rec.github}?s=192` : null } : undefined
}
export const PLAN_SENDER: Sender = { username: 'Staged deletions', icon_emoji: ':wastebasket:' }
export function personSender(email: string, person: SlackPerson | undefined, verb: string): Sender {
  const name = person?.name ?? email.replace(/@.*$/, '')
  return { username: `${name} · ${verb}`, ...(person?.image ? { icon_url: person.image } : { icon_emoji: ':bust_in_silhouette:' }) }
}

/** Prefixes with their numbers at one scan. */
export interface ScanStats { scan: string; stats: Record<string, PrefixStat> }

/** `prefixes` at the latest scan, every one of them: sorted (neighbours share
 *  row groups) and looked up `MAX_PREFIXES` at a time, as `/staged` chunks its
 *  `POST /api/prefixes`. Null when the index isn't readable here; a failed
 *  lookup throws (an unsized prefix is never counted as empty). */
export async function stagedStats(env: Env, prefixes: readonly string[], db: D1Database | null = null): Promise<ScanStats | null> {
  if (!prefixes.length || !storeReady(env)) return null
  const scans = (await pathScans(env, true)).results
  const scan = scans[scans.length - 1]?.date
  if (!scan) return null
  const sorted = [...new Set(prefixes)].sort()
  const stats: Record<string, PrefixStat> = {}
  for (let i = 0; i < sorted.length; i += MAX_PREFIXES) Object.assign(stats, (await prefixesAt(env, scan, sorted.slice(i, i + MAX_PREFIXES))).stats)
  // Given the plan's D1: a queue's sizes, less what's been deleted since this scan (an activity row's are the scan's own).
  return { scan, stats: db ? withoutDeleted(stats, await scanDeletions(db, scan)) : stats }
}

/** `prefixes`' totals and top owners (by attributed bytes) from `at`. */
export function sizeOf(at: ScanStats, prefixes: readonly string[], ownerName: (id: string) => string, topOwners = 4): Sized {
  let b = 0, o = 0, empty = 0
  const byOwner = new Map<string, number>()
  for (const p of prefixes) {
    const st = at.stats[p]
    if (emptyAt(st)) { empty++; continue }
    b += st.b; o += st.o
    for (const [u, ub] of st.us ?? []) byOwner.set(u, (byOwner.get(u) ?? 0) + ub)
  }
  const owners = [...byOwner].sort((x, y) => y[1] - x[1]).slice(0, topOwners).map(([u, ub]) => ({ label: ownerName(u), b: ub }))
  return { scan: at.scan, b, o, empty, owners }
}

/** A staged item, as the parent counts it. */
export interface QueuedItem { prefix: string; added_by: string; batch_id: number | null }

/** The staged items every count shows (the Slack parent's title, size line
 *  and the plan's card): those no real run `deleted`, less those empty at the
 *  scan (`stats`; null = unsized, so none is known empty). */
export function countedItems<T extends { prefix: string }>(items: readonly T[], deleted: ReadonlySet<string>, stats: Record<string, PrefixStat> | null): T[] {
  return items.filter(i => !deleted.has(i.prefix) && !(stats && emptyAt(stats[i.prefix])))
}

/** What the parent title counts of the `held` items (`countedItems`): their
 *  prefixes, batches, stagers. */
export function queuedOf(held: readonly QueuedItem[]): NonNullable<ParentView['queued']> {
  return {
    items: held.length,
    batches: new Set(held.map(i => i.batch_id).filter(b => b != null)).size,
    stagers: [...new Set(held.map(i => i.added_by))].sort(),
  }
}

async function loadView(db: D1Database, planId: number, siteUrl: string, actions: boolean): Promise<(ParentView & { slack_ts: string | null; slack_channel: string | null; deleted: Set<string>; prefixes: string[]; live: QueuedItem[] }) | null> {
  const plan = await db.prepare('SELECT id, state, slack_ts, slack_channel FROM plans WHERE id = ?').bind(planId)
    .first<{ id: number; state: string; slack_ts: string | null; slack_channel: string | null }>()
  if (!plan) return null
  const items = (await db.prepare('SELECT prefix, added_by, batch_id FROM plan_items WHERE plan_id = ?').bind(planId).all<QueuedItem>()).results
  const batches = await db.prepare('SELECT count(DISTINCT batch_id) AS n FROM plan_items WHERE plan_id = ? AND batch_id IS NOT NULL').bind(planId).first<{ n: number }>()
  const runs = await planRuns(db, planId)
  const deleted = await deletedItems(db, planId)
  const live = items.filter(i => !deleted.has(i.prefix))
  return {
    queued: queuedOf(live),
    live,
    deleted,
    prefixes: items.map(i => i.prefix),
    planId, siteUrl, actions, runs,
    items: items.length,
    batches: batches?.n ?? 0,
    stagers: [...new Set(items.map(i => i.added_by))].sort(),
    digest: await planDigest(await planItemLines(db, planId)),
    closed: plan.state !== 'open',
    slack_ts: plan.slack_ts,
    slack_channel: plan.slack_channel,
  }
}

/** Items a real run of the plan deleted since they were staged (not undone). */
export async function deletedItems(db: D1Database, planId: number): Promise<Set<string>> {
  const { results } = await db.prepare(`
    SELECT DISTINCT i.prefix FROM plan_items i JOIN deletion_bands b ON b.prefix = i.prefix
    JOIN deletion_runs r ON r.run_id = b.run_id
    WHERE i.plan_id = ? AND r.plan_id = i.plan_id AND r.mode = 'real' AND r.finished_ts IS NOT NULL
      AND r.started_ts >= i.added_ts AND COALESCE(b.undone_objects, 0) = 0
  `).bind(planId).all<{ prefix: string }>()
  return new Set(results.map(r => r.prefix))
}

/** Does the parent message carry a ✅? Read via `conversations.replies` (the
 *  bot has `channels:history` / `groups:history`, not `reactions:read`): its
 *  first message is the parent, with its `reactions`. Unreadable = no. */
export async function parentDone(env: SlackEnv, channel: string, ts: string): Promise<boolean> {
  if (!env.SLACK_BOT_TOKEN) return false
  try {
    const r = await fetch(`https://slack.com/api/conversations.replies?channel=${encodeURIComponent(channel)}&ts=${encodeURIComponent(ts)}&limit=1`, { headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}` } })
    const j = (await r.json().catch(() => null)) as { ok?: boolean; error?: string; messages?: { ts?: string; reactions?: { name: string }[] }[] } | null
    if (!j?.ok) { console.log(`slack conversations.replies failed: ${j?.error ?? `http ${r.status}`}`); return false }
    const parent = j.messages?.find(m => m.ts === ts)
    return !!parent?.reactions?.some(x => x.name === DONE_REACTION)
  } catch (e) {
    console.log(`slack conversations.replies threw: ${(e as Error).message}`)
    return false
  }
}

/** Detach the plan from thread `ts` (race-safe: only one caller wins). The
 *  loser gets the plan's thread as the winner left it. */
async function detach(db: D1Database, planId: number, ts: string): Promise<{ won: boolean; ts: string | null; channel: string | null }> {
  const cut = await db.prepare('UPDATE plans SET slack_ts = NULL WHERE id = ? AND slack_ts = ?').bind(planId, ts).run()
  if (cut.meta.changes) return { won: true, ts: null, channel: null }
  const row = await db.prepare('SELECT slack_channel, slack_ts FROM plans WHERE id = ?').bind(planId).first<{ slack_channel: string | null; slack_ts: string | null }>()
  return { won: false, ts: row?.slack_ts ?? null, channel: row?.slack_channel ?? null }
}

/** A stage reply's content from D1: a coalesced reply (`replyId`: its batches)
 *  or a pre-coalescing one (`batchId`). Null when it no longer exists. */
async function loadReply(db: D1Database, planId: number, ref: { replyId: number } | { batchId: number }): Promise<{ by: string; batches: number; prefixes: string[]; batchOf: Record<string, number>; note: string | null } | null> {
  const batches = 'replyId' in ref
    ? (await db.prepare('SELECT id, note, created_by FROM stage_batches WHERE reply_id = ? AND plan_id = ? ORDER BY id').bind(ref.replyId, planId).all<{ id: number; note: string | null; created_by: string }>()).results
    : (await db.prepare('SELECT id, note, created_by FROM stage_batches WHERE id = ? AND plan_id = ?').bind(ref.batchId, planId).all<{ id: number; note: string | null; created_by: string }>()).results
  if (!batches.length) return null
  const ids = batches.map(b => b.id)
  const items = (await db.prepare('SELECT prefix, batch_id FROM plan_items WHERE plan_id = ? AND batch_id IN (SELECT value FROM json_each(?)) ORDER BY prefix').bind(planId, JSON.stringify(ids)).all<{ prefix: string; batch_id: number }>()).results
  const note = [...batches].reverse().find(b => b.note)?.note ?? null
  return { by: batches[0].created_by, batches: batches.length, prefixes: items.map(i => i.prefix), batchOf: Object.fromEntries(items.map(i => [i.prefix, i.batch_id])), note }
}

interface ReplyCtx { planId: number; siteUrl: string; names: Names; stats: Record<string, PrefixStat> | null }
async function renderReply(db: D1Database, c: ReplyCtx, ref: { replyId: number } | { batchId: number }): Promise<{ text: string; blocks: unknown[] } | null> {
  const r = await loadReply(db, c.planId, ref)
  if (!r) return null
  return stageReply({ planId: c.planId, replyId: 'replyId' in ref ? ref.replyId : null, batchId: 'batchId' in ref ? ref.batchId : undefined, by: who(r.by, c.names), batches: r.batches, prefixes: r.prefixes, batchOf: r.batchOf, stats: c.stats, note: r.note, siteUrl: c.siteUrl })
}

/** A claimed reply not posted within this many seconds was claimed by a
 *  notify that died (a `waitUntil` cut short): the next announce re-claims. */
export const CLAIM_GRACE_S = 120

/** Batches of the plan that no posted or pending reply shows: never linked
 *  (`reply_id` NULL: their notify died before announcing), or linked to a
 *  reply closed without ever being posted. Bounded to batches still holding
 *  an item, staged no earlier than `COALESCE_S` before the deployment's first
 *  coalesced reply: older unlinked batches are pre-coalescing ones, announced
 *  by replies of their own. Binds `?1` = plan id, `?2` = now (the floor while
 *  there is no reply yet). */
const UNSHOWN = `b.plan_id = ?1
  AND (b.reply_id IS NULL OR b.reply_id IN (SELECT id FROM stage_replies WHERE slack_ts IS NULL AND open IS NULL))
  AND b.created_ts >= COALESCE((SELECT min(created_ts) FROM stage_replies), ?2) - ${COALESCE_S}
  AND EXISTS (SELECT 1 FROM plan_items i WHERE i.plan_id = b.plan_id AND i.batch_id = b.id)`

/** Stagers (lowercased) with batches no reply shows (`UNSHOWN`). */
export async function unshownStagers(db: D1Database, planId: number, now: number): Promise<string[]> {
  const { results } = await db.prepare(`SELECT DISTINCT lower(b.created_by) AS stager FROM stage_batches b WHERE ${UNSHOWN} ORDER BY stager`).bind(planId, now).all<{ stager: string }>()
  return results.map(r => r.stager)
}

/** Announce `stager`'s batches in thread `thread`: join their open reply
 *  there (updated within `COALESCE_S`) and edit it in place, or claim a new
 *  one and post it. The reply takes `batchId` (the stage being announced, if
 *  any) and every batch of theirs no reply shows (`UNSHOWN`), so a batch
 *  whose own notify died is announced by the next notify that runs — theirs,
 *  or anyone's (`notifyPlan`'s sweep). The claim is the `stage_replies_open`
 *  unique index (one open reply per plan and stager): concurrent stages
 *  insert, one wins, every one attaches its batch to the winner. The winner
 *  re-renders after recording its post's ts, and a joiner that finds that ts
 *  edits the reply itself, so every batch attached ends up shown. A claim
 *  left unposted past `CLAIM_GRACE_S` is closed and its batches re-adopted. */
async function announceStage(env: SlackEnv, db: D1Database, o: { channel: string; thread: string; batchId?: number; stager: string; now: number; ctx: ReplyCtx; sender: Sender }): Promise<void> {
  const { planId } = o.ctx
  const stager = o.stager.toLowerCase()
  await db.prepare(`UPDATE stage_replies SET open = NULL WHERE plan_id = ? AND stager = ? AND open = 1
    AND (channel != ? OR thread_ts != ? OR updated_ts < ? OR (slack_ts IS NULL AND created_ts < ?))`)
    .bind(planId, stager, o.channel, o.thread, o.now - COALESCE_S, o.now - CLAIM_GRACE_S).run()
  const openReply = () => db.prepare('SELECT id, slack_ts FROM stage_replies WHERE plan_id = ? AND stager = ? AND open = 1').bind(planId, stager).first<{ id: number; slack_ts: string | null }>()
  let r = await openReply()
  let claimed = false
  if (!r) {
    const ins = await db.prepare('INSERT OR IGNORE INTO stage_replies (plan_id, stager, channel, thread_ts, open, created_ts, updated_ts) VALUES (?, ?, ?, ?, 1, ?, ?)')
      .bind(planId, stager, o.channel, o.thread, o.now, o.now).run()
    claimed = ins.meta.changes > 0
    r = await openReply()
  }
  if (!r) return
  if (o.batchId != null) await db.prepare('UPDATE stage_batches SET reply_id = ? WHERE id = ? AND plan_id = ?').bind(r.id, o.batchId, planId).run()
  await db.prepare(`UPDATE stage_batches AS b SET reply_id = ?3 WHERE ${UNSHOWN} AND lower(b.created_by) = ?4`).bind(planId, o.now, r.id, stager).run()
  await db.prepare('UPDATE stage_replies SET updated_ts = max(updated_ts, ?) WHERE id = ?').bind(o.now, r.id).run()
  const ref = { replyId: r.id }
  // Edit the reply until what it shows is what D1 holds (a joiner may attach
  // a batch between a render and its update).
  const settle = async (ts: string, shown: string | null) => {
    for (let i = 0; i < 3; i++) {
      const m = await renderReply(db, o.ctx, ref)
      if (!m || JSON.stringify(m.blocks) === shown) return
      await slackApi(env, 'chat.update', { channel: o.channel, ts, ...m })
      shown = JSON.stringify(m.blocks)
    }
  }
  if (claimed) {
    const m = await renderReply(db, o.ctx, ref)
    const p = m ? await slackApi(env, 'chat.postMessage', { channel: o.channel, thread_ts: o.thread, ...m, ...o.sender, unfurl_links: false }) : null
    if (!m || !p?.ok || !p.ts) {
      // Nothing to show (a sweep raced the batch's own notify) or the post
      // failed: close the claim, so the next announce claims anew (after a
      // failed post, re-adopting its batches).
      await db.prepare('UPDATE stage_replies SET open = NULL WHERE id = ?').bind(r.id).run()
      return
    }
    await db.prepare('UPDATE stage_replies SET slack_ts = ? WHERE id = ?').bind(p.ts, r.id).run()
    await settle(p.ts, JSON.stringify(m.blocks))
  } else if (r.slack_ts) {
    await settle(r.slack_ts, null)
  }
}

/** A cost bound on how many times one notify re-renders a thread, not something correctness rests on: a change it misses has its own notify, which sends later and checks again. */
const SETTLE_ROUNDS = 3

/** Re-render the plan's parent message (posting it first if the plan has
 * none) and, with `event`, reply in its thread. Before replying, a thread
 * whose parent carries a ✅ (or a stage into an emptied queue) moves the plan
 * to a new thread: a new parent, a pointer in the old thread, and the old
 * parent marked done. A thread in another channel than `SLACK_ADMIN_CHANNEL`
 * is left alone and a new one starts there. Best-effort; never throws. */
export async function notifyPlan(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, ev?: Event | StageArgs | RunArgs, opts: { now?: number; quiet?: boolean } = {}): Promise<void> {
  if (!slackReady(env)) return
  const now = opts.now ?? Math.floor(Date.now() / 1000)
  try {
    const v = await loadView(db, planId, siteUrl, !!env.GCP_SA_KEY)
    if (!v) return
    const full = env as NotifyEnv & Env
    const stage = ev && 'stage' in ev ? ev.stage : null
    const runEv = ev && 'run' in ev ? ev : null
    const said = ev && !('stage' in ev) && !('run' in ev) ? ev as Event : null
    const by = stage?.by ?? (runEv?.phase === 'dispatched' ? runEv.run.actor : said?.by?.email ?? null)
    const render = async (v: NonNullable<Awaited<ReturnType<typeof loadView>>>) => {
      const queued = v.prefixes.filter(p => !v.deleted.has(p))
      const [at, reg, people] = await Promise.all([
        stagedStats(full, queued, db).catch(e => { console.log(`staged slack sizing failed for plan ${planId}: ${(e as Error).message}`); return null }),
        loadRegistry(full).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>),
        slackPeople(env, [...v.stagers, ...v.runs.map(r => r.actor), ...(by ? [by] : [])]),
      ])
      // The workspace's members first; anyone it doesn't know (a sign-in email outside it), as the site names them.
      for (const e of [...v.stagers, ...v.runs.map(r => r.actor), ...(by ? [by] : [])]) {
        const k = e.toLowerCase()
        const r = people[k] ? undefined : registryPerson(reg, e)
        if (r) people[k] = r
      }
      const names: Names = Object.fromEntries(Object.entries(people).flatMap(([e, p]) => p.name ? [[e, p.name]] : []))
      const size = at ? sizeOf(at, queued, u => reg[u]?.name ?? u) : undefined
      // Sized, prefixes empty at the scan leave the title's counts (the size line names them apart).
      const last = lastRealRun(v.runs)
      const lastOutcome = last?.finished_ts ? await lastRunOutcome(full)?.(last) : null
      // Only the `sweep` executor's undo restores from soft delete: `plan-sweep`'s card says what it always did.
      const undo = env.EXECUTOR === 'sweep' && last?.finished_ts ? await runUndoNow(full, db, last, now) : null
      const parent = renderParent({ ...v, lastOutcome, undoUnavailable: undo?.unavailable, undoUnseen: !!undo?.unseen, now, queued: queuedOf(countedItems(v.live, v.deleted, at?.stats ?? null)), names, size, image: await stagedCardUrl(env, db, siteUrl, v.digest) ?? undefined })
      return { at, people, names, parent }
    }
    const { at, people, names, parent } = await render(v)
    // A run's own end: what it measured stands, unless the reading already shows soft delete off where it deleted.
    const runUndo = runEv && runEv.phase !== 'dispatched' && runEv.run.mode === 'real' && env.EXECUTOR === 'sweep' ? await runUndoNow(full, db, runEv.run, now) : null
    const event: Event | undefined = runEv
      ? { text: runEvent(runEv.run, runEv.phase, { via: runEv.via, names, siteUrl, reason: runEv.reason, outcome: runEv.outcome, expected: runEv.expected, undoUnavailable: runUndo?.unavailable }), ...(by ? { sender: personSender(by, people[by.toLowerCase()], 'dispatched') } : {}) }
      : said ? { ...said, text: typeof said.text === 'function' ? said.text(who(said.by?.email ?? '', names)) : said.text, ...(said.by ? { sender: personSender(said.by.email, people[said.by.email.toLowerCase()], said.by.verb) } : {}) }
      : undefined
    const channel = env.SLACK_ADMIN_CHANNEL!
    let ts = v.slack_ts
    if (ts && v.slack_channel !== channel) {
      const d = await detach(db, planId, ts)
      ts = d.won || d.channel !== channel ? null : d.ts
    }
    // The thread this event ends, and why (`ended`).
    let old: string | null = null
    let ended = ''
    if (ts && ev) {
      const stats = at ? Object.fromEntries(Object.entries(at.stats).map(([p, s]) => [p, s.b])) : null
      if (stage && queueWasEmpty(v.prefixes, new Set(stage.prefixes), v.deleted, stats)) ended = QUEUE_EMPTIED
      else if (await parentDone(env, channel, ts)) ended = ':white_check_mark: Marked done.'
      if (ended) {
        const d = await detach(db, planId, ts)
        if (d.won) { old = ts; ts = null } else ts = d.channel === channel ? d.ts : null
      }
    }
    if (!ts) {
      const p = await sendParent(env, channel, null, parent)
      if (!p.ok || !p.ts) return
      // Two concurrent first events would each post a parent: the claim is
      // the tiebreak, and the loser deletes its own.
      const claim = await db.prepare('UPDATE plans SET slack_channel = ?, slack_ts = ? WHERE id = ? AND slack_ts IS NULL').bind(channel, p.ts, planId).run()
      if (claim.meta.changes) ts = p.ts
      else {
        await slackApi(env, 'chat.delete', { channel, ts: p.ts })
        const row = await db.prepare('SELECT slack_channel, slack_ts FROM plans WHERE id = ?').bind(planId).first<{ slack_channel: string; slack_ts: string }>()
        if (!row?.slack_ts || row.slack_channel !== channel) return
        ts = row.slack_ts
        await sendParent(env, channel, ts, parent)
      }
    } else {
      await sendParent(env, channel, ts, parent)
    }
    if (old) {
      const pl = await slackApi(env, 'chat.getPermalink', { channel, message_ts: ts })
      const link = pl.ok && typeof pl.permalink === 'string' ? pl.permalink : null
      await slackApi(env, 'chat.postMessage', { channel, thread_ts: old, text: `${ended} Continued in ${link ? `<${link}|a new thread>` : 'a new thread'}.`, ...PLAN_SENDER, unfurl_links: false })
      await slackApi(env, 'chat.update', { channel, ts: old, ...closedParent(planId, siteUrl, link) })
    }
    const ctx: ReplyCtx = { planId, siteUrl, names, stats: at?.stats ?? null }
    if (stage) {
      await announceStage(env, db, {
        channel, thread: ts, batchId: stage.batchId, stager: stage.by, now, ctx,
        sender: personSender(stage.by, people[stage.by.toLowerCase()], 'staged'),
      })
    } else if (event) {
      await slackApi(env, 'chat.postMessage', { channel, thread_ts: ts, text: event.text as string, ...(event.blocks ? { blocks: event.blocks } : {}), ...(event.sender ?? PLAN_SENDER), unfurl_links: false })
    }
    // Batches no reply shows (their own notify died) are announced now, by
    // whichever notify runs next, each in its stager's reply.
    if (!opts.quiet) for (const s of await unshownStagers(db, planId, now)) {
      await announceStage(env, db, { channel, thread: ts, stager: s, now, ctx, sender: personSender(s, people[s.toLowerCase()], 'staged') })
    }
    // Stages landing together have notifies that interleave, and whichever sends last wins whatever it rendered. So each
    // checks after sending: if the queue has moved on it renders again, and the last sender can't leave a stale thread.
    let shown = v.digest
    for (let i = 0; i < SETTLE_ROUNDS; i++) {
      const now2 = await loadView(db, planId, siteUrl, !!env.GCP_SA_KEY)
      if (!now2 || now2.digest === shown || now2.slack_ts !== ts) break
      shown = now2.digest
      const again = await render(now2)
      await sendParent(env, channel, ts, again.parent)
      if (stage) await announceStage(env, db, {
        channel, thread: ts, stager: stage.by, now, ctx: { planId, siteUrl, names: again.names, stats: again.at?.stats ?? null },
        sender: personSender(stage.by, again.people[stage.by.toLowerCase()], 'staged'),
      })
    }
  } catch (e) {
    console.log(`staged slack notify failed for plan ${planId}: ${(e as Error).message}`)
  }
}

/** Re-render stage replies in place (`chat.update`: no one is notified): the
 *  coalesced ones (`replyIds`; all of the plan's posted ones in this channel
 *  when omitted) and pre-coalescing single-batch ones (`legacy`: batch id →
 *  reply ts). Returns each one's outcome. */
export async function updateReplies(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, { replyIds, legacy = {} }: { replyIds?: number[]; legacy?: Record<number, string> } = {}): Promise<{ replies: Record<string, string>; coalesced: Record<string, string> }> {
  const out = { replies: {} as Record<string, string>, coalesced: {} as Record<string, string> }
  if (!slackReady(env)) return out
  const channel = env.SLACK_ADMIN_CHANNEL!
  const rows = replyIds
    ? (await db.prepare(`SELECT id, channel, slack_ts FROM stage_replies WHERE plan_id = ? AND id IN (${replyIds.map(() => '?').join(',') || 'NULL'})`).bind(planId, ...replyIds).all<{ id: number; channel: string; slack_ts: string | null }>()).results
    : (await db.prepare('SELECT id, channel, slack_ts FROM stage_replies WHERE plan_id = ? AND slack_ts IS NOT NULL').bind(planId).all<{ id: number; channel: string; slack_ts: string | null }>()).results
  const posted = rows.filter((r): r is typeof r & { slack_ts: string } => r.slack_ts != null && r.channel === channel)
  const plan = await db.prepare('SELECT slack_channel FROM plans WHERE id = ?').bind(planId).first<{ slack_channel: string | null }>()
  const legacyTs = plan?.slack_channel === channel ? Object.entries(legacy) : []
  if (!posted.length && !legacyTs.length) return out
  const items = (await db.prepare('SELECT prefix, added_by FROM plan_items WHERE plan_id = ?').bind(planId).all<{ prefix: string; added_by: string }>()).results
  const batchBy = (await db.prepare('SELECT created_by FROM stage_batches WHERE plan_id = ?').bind(planId).all<{ created_by: string }>()).results.map(b => b.created_by)
  const [at, names] = await Promise.all([
    stagedStats(env as NotifyEnv & Env, items.map(i => i.prefix), db).catch(() => null),
    slackNames(env, [...new Set(batchBy)]),
  ])
  const ctx: ReplyCtx = { planId, siteUrl, names, stats: at?.stats ?? null }
  for (const r of posted) {
    const m = await renderReply(db, ctx, { replyId: r.id })
    if (!m) { out.coalesced[r.id] = 'no batches'; continue }
    const u = await slackApi(env, 'chat.update', { channel, ts: r.slack_ts, ...m })
    out.coalesced[r.id] = u.ok ? 'updated' : (u.error ?? 'failed')
  }
  for (const [batch, ts] of legacyTs) {
    const m = await renderReply(db, ctx, { batchId: Number(batch) })
    if (!m) { out.replies[batch] = 'no such batch'; continue }
    const u = await slackApi(env, 'chat.update', { channel, ts, ...m })
    out.replies[batch] = u.ok ? 'updated' : (u.error ?? 'failed')
  }
  return out
}

/** Re-render the plan's parent and every stage reply in place (`chat.update`:
 * no one is notified) — the coalesced ones from `stage_replies`, plus the
 * pre-coalescing single-batch ones named by `replies` (batch id → ts).
 * Returns what was updated. */
export async function refreshThread(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, replies: Record<number, string>): Promise<{ parent: boolean; replies: Record<string, string>; coalesced: Record<string, string> }> {
  const out = { parent: false, replies: {} as Record<string, string>, coalesced: {} as Record<string, string> }
  if (!slackReady(env)) return out
  const plan = await db.prepare('SELECT slack_ts, slack_channel FROM plans WHERE id = ?').bind(planId).first<{ slack_ts: string | null; slack_channel: string | null }>()
  if (!plan?.slack_ts || plan.slack_channel !== env.SLACK_ADMIN_CHANNEL) return out
  await notifyPlan(env, db, planId, siteUrl)
  out.parent = true
  return { ...out, ...await updateReplies(env, db, planId, siteUrl, { legacy: replies }) }
}

/** A new scan has landed, and only an event re-renders the card: the job's post-scan ping (`dt-cloud staged-card`) brings the
 *  open plan's card up to it. An edit in place and nothing else: no card without a thread yet, none in another channel, no
 *  reply for a batch a died notify left unshown. Says whether it edited. */
export async function refreshStagedCard(env: NotifyEnv, db: D1Database, siteUrl: string): Promise<boolean> {
  if (!slackReady(env)) return false
  const id = await openPlanId(db)
  if (id == null) return false
  const plan = await db.prepare('SELECT slack_ts, slack_channel FROM plans WHERE id = ?').bind(id).first<{ slack_ts: string | null; slack_channel: string | null }>()
  if (!plan?.slack_ts || plan.slack_channel !== env.SLACK_ADMIN_CHANNEL) return false
  await notifyPlan(env, db, id, siteUrl, undefined, { quiet: true })
  return true
}

/** Announce runs that just finished (an executor's reflection) in their
 * plans' threads: the totals, or that the run ended without a result. */
export async function announceFinished(env: NotifyEnv, db: D1Database, runs: readonly FinishedRun[], siteUrl: string): Promise<void> {
  for (const { run_id, ok, reason, outcome } of runs) {
    const r = await db.prepare('SELECT * FROM deletion_runs WHERE run_id = ?').bind(run_id).first<RunRow & { plan_id: number | null }>()
    if (r?.plan_id == null) continue
    // What the run deleted leaves the queue before the thread re-renders.
    if (ok) await unstageDeleted(db, r.plan_id, [run_id])
    await notifyPlan(env, db, r.plan_id, siteUrl, { run: r, phase: ok ? 'finished' : 'failed', reason, outcome })
  }
}
