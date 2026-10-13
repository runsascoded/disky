/**
 * The staged-deletion review loop in Slack (specs/done/staged-slack.md): one thread
 * per staged plan in the deployment's admin channel (`SLACK_ADMIN_CHANNEL`,
 * the only place a channel comes from). The parent message is re-rendered on
 * every event (counts, size, the latest dry-run, whether it still matches the
 * plan, the action buttons); each event is a reply. A stager's stages within
 * `COALESCE_S` of their reply's last update edit that one reply in place. A ✅
 * on the parent (or an emptied queue) moves the plan to a new thread on the
 * next event. Automatic posts name people in plain text, never by mention.
 * The pure parts (`renderParent`, `stageReply`, the event texts; the gate is
 * `plans.realGate`) are what the unit tests pin; the rest is Slack + D1 I/O,
 * best-effort — a Slack failure never fails the gesture that caused it.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type FinishedRun, type Gate, planDigest, planItemLines, planRuns, realGate, type RunRow, unstageDeleted } from './plans.js'
import { slackApi, slackReady, type SlackEnv } from './slack.js'
import type { Env } from './auth.js'
import { pathScans, storeReady } from './index.js'
import { MAX_PREFIXES, type PrefixStat, prefixesAt } from './prefixes.js'
import { expDay, IMAGE_TTL_DAYS, imagePath, ogKey } from './og/sign.js'
import { imageParams } from './og/cred.js'
import { serverToken } from './og/tokens.js'
import { loadRegistry } from './identity.js'

export type { RunRow }

export const fmtBytes = (n: number): string => {
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${i ? v.toFixed(1) : v} ${u[i]}`
}
const fmtN = (n: number): string => n.toLocaleString('en-US')
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
  /** Offer the dispatch buttons (the deployment's executor is wired for Slack). */
  actions: boolean
  closed: boolean
  names?: Names
  /** The staged set's size at the latest scan. */
  size?: Sized
  /** The plan's share card (a signed, full-tier `/og/staged.png` URL). */
  image?: string
  /** What is still queued: the items no real run has deleted (the title and
   *  "staged by"; the gate and the confirm stay on the whole plan). */
  queued?: { items: number; batches: number; stagers: string[] }
}

/** A staged set at one scan: totals and its largest owners (by display name). */
export interface Sized {
  scan: string
  b: number
  o: number
  /** Prefixes with nothing left at `scan`. */
  empty: number
  owners: { label: string; b: number }[]
}

const who = (email: string, names?: Names): string => names?.[email.toLowerCase()] ?? email.replace(/@.*$/, '')

/** `51.0 TiB · 179,327,698 objects at scan 2026-10-02 · owners: Grace Hopper 16.0 TiB, hedy 6.0 TiB`. */
export function sizeLine(z: Sized): string {
  const owners = z.owners.length ? ` · owners: ${z.owners.map(o => `${o.label} ${fmtBytes(o.b)}`).join(', ')}` : ''
  const empty = z.empty ? ` · ${fmtN(z.empty)} empty` : ''
  return `*${fmtBytes(z.b)}* · ${fmtN(z.o)} objects at scan ${z.scan}${empty}${owners}`
}

/** The thread's parent message: `{ text, blocks }` for chat.postMessage / chat.update. */
export function renderParent(v: ParentView): { text: string; blocks: unknown[] } {
  const q = v.queued ?? { items: v.items, batches: v.batches, stagers: v.stagers }
  const title = v.closed
    ? `*Staged plan #${v.planId}* (closed)`
    : `*Staged for deletion* · plan #${v.planId} · ${fmtN(q.items)} ${q.items === 1 ? 'prefix' : 'prefixes'} in ${q.batches} ${q.batches === 1 ? 'batch' : 'batches'}`
  const by = (q.stagers.length ? `staged by ${q.stagers.map(e => who(e, v.names)).join(', ')}` : 'nothing staged')
    + (v.size ? `\n${sizeLine(v.size)}` : '')
  const gate = realGate(v.runs, v.digest, v.items)
  const latestDry = [...v.runs].filter(r => r.mode === 'dry').sort((a, b) => b.started_ts - a.started_ts)[0]
  let dryLine: string
  if (!latestDry) dryLine = 'No dry-run yet.'
  else if (latestDry.finished_ts == null) dryLine = `Dry-run running (${who(latestDry.actor, v.names)}, scan ${latestDry.scan}).`
  else if (latestDry.plan_digest === '') dryLine = `Latest dry-run (\`${latestDry.run_id}\`) ended without a result.`
  else {
    const res = `would delete *${fmtBytes(latestDry.deleted_bytes)}* / ${fmtN(latestDry.deleted_objects)} objects (scan ${latestDry.scan})`
    dryLine = latestDry.plan_digest === v.digest
      ? `Latest dry-run ${res} — matches the current plan.`
      : `Latest dry-run ${res} — *stale*: the plan changed since.`
  }
  const lastReal = [...v.runs].filter(r => r.mode === 'real').sort((a, b) => b.started_ts - a.started_ts)[0]
  const realLine = lastReal
    ? lastReal.finished_ts == null
      ? `\nReal run in progress (${who(lastReal.actor, v.names)}).`
      : `\nLast real run deleted ${fmtBytes(lastReal.deleted_bytes)} / ${fmtN(lastReal.deleted_objects)} objects${lastReal.undo_deadline ? `, undoable until ${utc(lastReal.undo_deadline)}` : ''}.`
    : ''
  const buttons: unknown[] = [
    { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'Open in www' }, url: `${v.siteUrl}/staged` },
  ]
  if (v.actions && !v.closed && v.items) {
    buttons.push({ type: 'button', action_id: 'staged_dry', value: String(v.planId), text: { type: 'plain_text', text: 'Dry-run' } })
    if (gate.ok) {
      buttons.push({
        type: 'button', action_id: 'staged_real', value: `${v.planId}:${v.digest}`, style: 'danger',
        text: { type: 'plain_text', text: 'Delete for real…' },
        confirm: {
          title: { type: 'plain_text', text: 'Really delete?' },
          text: { type: 'mrkdwn', text: `Deletes the ${fmtN(v.items)} staged ${v.items === 1 ? 'prefix' : 'prefixes'}: ${fmtBytes(gate.dry.deleted_bytes)} / ${fmtN(gate.dry.deleted_objects)} objects per the dry-run on scan ${gate.dry.scan}. Recoverable for 7 days (undo in www).` },
          confirm: { type: 'plain_text', text: 'Delete' },
          deny: { type: 'plain_text', text: 'Cancel' },
          style: 'danger',
        },
      })
    }
  }
  const hint = v.actions && !v.closed && v.items && !gate.ok ? `\n_Delete for real_ appears after a finished dry-run of the current set (${gate.reason}).` : ''
  const blocks = [
    { type: 'section', text: { type: 'mrkdwn', text: `${title}\n${by}` } },
    { type: 'section', text: { type: 'mrkdwn', text: `${dryLine}${realLine}${hint}` } },
    { type: 'actions', elements: buttons },
    ...(v.image && v.items ? [{ type: 'image', image_url: v.image, alt_text: `Plan #${v.planId}: the staged prefixes as a treemap, coloured by owner` }] : []),
  ]
  return { text: `Staged for deletion: plan #${v.planId}, ${v.items} prefixes`, blocks }
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
  batches: number
  /** What its batches still have staged. */
  prefixes: string[]
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
  let b = 0, o = 0
  for (const p of v.prefixes) { b += v.stats?.[p]?.b ?? 0; o += v.stats?.[p]?.o ?? 0 }
  const inBatches = v.batches > 1 ? ` in ${plural(v.batches, 'batch', 'batches')}` : ''
  const size = v.stats ? ` · *${fmtBytes(b)}* · ${plural(o, 'object')}` : ''
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
  const many = v.batches > 1
  const reject = {
    type: 'button', action_id: 'staged_reject', value: v.replyId != null ? `${v.planId}:r${v.replyId}` : `${v.planId}:${v.batchId}`,
    text: { type: 'plain_text', text: many ? `Reject ${fmtN(v.batches)} batches` : 'Reject batch' },
    confirm: {
      title: { type: 'plain_text', text: many ? `Reject these ${fmtN(v.batches)} batches?` : 'Reject this batch?' },
      text: { type: 'mrkdwn', text: `Unstages the ${plural(n, 'path')} ${many ? 'they' : 'it'} staged. Nothing is deleted.` },
      confirm: { type: 'plain_text', text: 'Reject' },
      deny: { type: 'plain_text', text: 'Cancel' },
    },
  }
  return {
    text: `${v.by} staged ${plural(n, 'path')}${inBatches}${v.stats ? ` · ${fmtBytes(b)}` : ''}`,
    blocks: [
      { type: 'section', text: { type: 'mrkdwn', text: `:wastebasket: *${v.by}* staged ${plural(n, 'path')}${inBatches}${size}\n${topFolders(v.prefixes, v.stats)}${note}` } },
      { type: 'actions', elements: [reject, view] },
    ],
  }
}

/** A run's page on the site: `/staged?run=<id>` opens and scrolls to its row
 *  (the run id, or its Batch job's id before the executor records it). */
export const runUrl = (siteUrl: string, runId: string): string => `${siteUrl}/staged?run=${encodeURIComponent(runId)}`

/** A run's thread reply: the actor by name, the run id linking to its row on
 *  `/staged`. */
export function runEvent(r: RunRow, phase: 'dispatched' | 'finished' | 'failed', o: { via?: string | null; names?: Names; siteUrl?: string } = {}): string {
  const kind = r.mode === 'dry' ? 'Dry-run' : '*Real deletion*'
  const id = o.siteUrl ? `<${runUrl(o.siteUrl, r.run_id)}|${r.run_id}>` : `\`${r.run_id}\``
  if (phase === 'dispatched') return `${r.mode === 'dry' ? ':test_tube:' : ':rotating_light:'} ${kind} dispatched by ${who(r.actor, o.names)}${o.via ? ` via ${o.via}` : ''} on scan ${r.scan} (${id})`
  if (phase === 'failed') return `:x: ${kind} ${id} ended without a result (its Batch job stopped before the run summary); check its logs in www.`
  if (r.mode === 'dry') return `:test_tube: Dry-run ${id} finished: would delete *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects (gone since scan: ${fmtN(r.skipped_gone)}, overwritten: ${fmtN(r.skipped_overwritten)}).`
  return `:white_check_mark: Real deletion ${id} finished: deleted *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects${r.undo_deadline ? `; undoable until ${utc(r.undo_deadline)} (www)` : ''}.`
}

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
export type NotifyEnv = SlackEnv & { GCP_SA_KEY?: string }

/** A stager's stages within this many seconds of their reply's last update
 *  edit that reply instead of posting a new one. */
export const COALESCE_S = 15 * 60

/** The parent reaction that closes a thread: the next event starts a new one. */
export const DONE_REACTION = 'white_check_mark'

interface Event { text: string; blocks?: unknown[]; sender?: Sender }
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
export interface RunArgs { run: RunRow; phase: 'dispatched' | 'finished' | 'failed'; via?: string | null }

/** A workspace member, as `users.lookupByEmail` returns them. */
export interface SlackPerson { name: string | null; image: string | null }

// email → member (or null: not in the workspace), per isolate.
const personMemo = new Map<string, Promise<SlackPerson | null>>()

/** Workspace members for `emails` (`users.lookupByEmail`; the bot has
 *  `users:read.email`). Unknown emails are left out. */
export async function slackPeople(env: SlackEnv, emails: readonly string[]): Promise<Record<string, SlackPerson>> {
  const out: Record<string, SlackPerson> = {}
  await Promise.all([...new Set(emails.map(e => e.toLowerCase()))].map(async e => {
    let m = personMemo.get(e)
    if (!m) {
      m = (async () => {
        if (!env.SLACK_BOT_TOKEN) return null
        const r = await fetch(`https://slack.com/api/users.lookupByEmail?email=${encodeURIComponent(e)}`, { headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}` } })
        const j = (await r.json().catch(() => null)) as { ok?: boolean; user?: { id?: string; deleted?: boolean; real_name?: string; profile?: { real_name?: string; image_192?: string } } } | null
        const u = j?.ok ? j.user : undefined
        return u?.id && !u.deleted ? { name: u.profile?.real_name || u.real_name || null, image: u.profile?.image_192 ?? null } : null
      })().catch(() => null)
      personMemo.set(e, m)
    }
    const v = await m
    if (v) out[e] = v
  }))
  return out
}

/** How automatic posts name `emails`: their Slack names where the workspace
 *  knows them (callers fall back to the local part). */
export async function slackNames(env: SlackEnv, emails: readonly string[]): Promise<Names> {
  const people = await slackPeople(env, emails)
  return Object.fromEntries(Object.entries(people).flatMap(([e, p]) => p.name ? [[e, p.name]] : []))
}

/** Who a message posts as (`username` / `icon_*`; honoured only with the
 *  `chat:write.customize` scope, ignored otherwise). The thread's parent is
 *  the plan itself; an event posts as the person who did it. */
export interface Sender { username: string; icon_url?: string; icon_emoji?: string }
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
export async function stagedStats(env: Env, prefixes: readonly string[]): Promise<ScanStats | null> {
  if (!prefixes.length || !storeReady(env)) return null
  const scans = (await pathScans(env, true)).results
  const scan = scans[scans.length - 1]?.date
  if (!scan) return null
  const sorted = [...new Set(prefixes)].sort()
  const stats: Record<string, PrefixStat> = {}
  for (let i = 0; i < sorted.length; i += MAX_PREFIXES) Object.assign(stats, (await prefixesAt(env, scan, sorted.slice(i, i + MAX_PREFIXES))).stats)
  return { scan, stats }
}

/** `prefixes`' totals and top owners (by attributed bytes) from `at`. */
export function sizeOf(at: ScanStats, prefixes: readonly string[], ownerName: (id: string) => string, topOwners = 4): Sized {
  let b = 0, o = 0, empty = 0
  const byOwner = new Map<string, number>()
  for (const p of prefixes) {
    const st = at.stats[p]
    if (!st || !st.b) { empty++; continue }
    b += st.b; o += st.o
    for (const [u, ub] of st.us ?? []) byOwner.set(u, (byOwner.get(u) ?? 0) + ub)
  }
  const owners = [...byOwner].sort((x, y) => y[1] - x[1]).slice(0, topOwners).map(([u, ub]) => ({ label: ownerName(u), b: ub }))
  return { scan: at.scan, b, o, empty, owners }
}

async function loadView(db: D1Database, planId: number, siteUrl: string, actions: boolean): Promise<(ParentView & { slack_ts: string | null; slack_channel: string | null; deleted: Set<string>; prefixes: string[] }) | null> {
  const plan = await db.prepare('SELECT id, state, slack_ts, slack_channel FROM plans WHERE id = ?').bind(planId)
    .first<{ id: number; state: string; slack_ts: string | null; slack_channel: string | null }>()
  if (!plan) return null
  const items = (await db.prepare('SELECT prefix, added_by, batch_id FROM plan_items WHERE plan_id = ?').bind(planId).all<{ prefix: string; added_by: string; batch_id: number | null }>()).results
  const batches = await db.prepare('SELECT count(DISTINCT batch_id) AS n FROM plan_items WHERE plan_id = ? AND batch_id IS NOT NULL').bind(planId).first<{ n: number }>()
  const runs = await planRuns(db, planId)
  const deleted = await deletedItems(db, planId)
  const live = items.filter(i => !deleted.has(i.prefix))
  return {
    queued: {
      items: live.length,
      batches: new Set(live.map(i => i.batch_id).filter(b => b != null)).size,
      stagers: [...new Set(live.map(i => i.added_by))].sort(),
    },
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
async function deletedItems(db: D1Database, planId: number): Promise<Set<string>> {
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
async function loadReply(db: D1Database, planId: number, ref: { replyId: number } | { batchId: number }): Promise<{ by: string; batches: number; prefixes: string[]; note: string | null } | null> {
  const batches = 'replyId' in ref
    ? (await db.prepare('SELECT id, note, created_by FROM stage_batches WHERE reply_id = ? AND plan_id = ? ORDER BY id').bind(ref.replyId, planId).all<{ id: number; note: string | null; created_by: string }>()).results
    : (await db.prepare('SELECT id, note, created_by FROM stage_batches WHERE id = ? AND plan_id = ?').bind(ref.batchId, planId).all<{ id: number; note: string | null; created_by: string }>()).results
  if (!batches.length) return null
  const ids = batches.map(b => b.id)
  const prefixes = (await db.prepare(`SELECT prefix FROM plan_items WHERE plan_id = ? AND batch_id IN (${ids.map(() => '?').join(',')}) ORDER BY prefix`).bind(planId, ...ids).all<{ prefix: string }>()).results.map(r => r.prefix)
  const note = [...batches].reverse().find(b => b.note)?.note ?? null
  return { by: batches[0].created_by, batches: batches.length, prefixes, note }
}

interface ReplyCtx { planId: number; siteUrl: string; names: Names; stats: Record<string, PrefixStat> | null }
async function renderReply(db: D1Database, c: ReplyCtx, ref: { replyId: number } | { batchId: number }): Promise<{ text: string; blocks: unknown[] } | null> {
  const r = await loadReply(db, c.planId, ref)
  if (!r) return null
  return stageReply({ planId: c.planId, replyId: 'replyId' in ref ? ref.replyId : null, batchId: 'batchId' in ref ? ref.batchId : undefined, by: who(r.by, c.names), batches: r.batches, prefixes: r.prefixes, stats: c.stats, note: r.note, siteUrl: c.siteUrl })
}

/** Announce stage batch `batchId` in thread `thread`: join its stager's open
 *  reply there (updated within `COALESCE_S`) and edit it in place, or claim a
 *  new one and post it. The claim is the `stage_replies_open` unique index
 *  (one open reply per plan and stager): concurrent stages insert, one wins,
 *  every one attaches its batch to the winner. The winner re-renders after
 *  recording its post's ts, and a joiner that finds that ts edits the reply
 *  itself, so every batch attached ends up shown. */
async function announceStage(env: SlackEnv, db: D1Database, o: { channel: string; thread: string; batchId: number; by: string; now: number; ctx: ReplyCtx; sender: Sender }): Promise<void> {
  const { planId } = o.ctx
  const stager = o.by.toLowerCase()
  await db.prepare('UPDATE stage_replies SET open = NULL WHERE plan_id = ? AND stager = ? AND open = 1 AND (channel != ? OR thread_ts != ? OR updated_ts < ?)')
    .bind(planId, stager, o.channel, o.thread, o.now - COALESCE_S).run()
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
  await db.prepare('UPDATE stage_batches SET reply_id = ? WHERE id = ? AND plan_id = ?').bind(r.id, o.batchId, planId).run()
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
    if (!m) return
    const p = await slackApi(env, 'chat.postMessage', { channel: o.channel, thread_ts: o.thread, ...m, ...o.sender, unfurl_links: false })
    if (!p.ok || !p.ts) {
      await db.prepare('UPDATE stage_replies SET open = NULL WHERE id = ?').bind(r.id).run()
      return
    }
    await db.prepare('UPDATE stage_replies SET slack_ts = ? WHERE id = ?').bind(p.ts, r.id).run()
    await settle(p.ts, JSON.stringify(m.blocks))
  } else if (r.slack_ts) {
    await settle(r.slack_ts, null)
  }
}

/** Re-render the plan's parent message (posting it first if the plan has
 * none) and, with `event`, reply in its thread. Before replying, a thread
 * whose parent carries a ✅ (or a stage into an emptied queue) moves the plan
 * to a new thread: a new parent, a pointer in the old thread, and the old
 * parent marked done. A thread in another channel than `SLACK_ADMIN_CHANNEL`
 * is left alone and a new one starts there. Best-effort; never throws. */
export async function notifyPlan(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, ev?: Event | StageArgs | RunArgs, opts: { now?: number } = {}): Promise<void> {
  if (!slackReady(env)) return
  const now = opts.now ?? Math.floor(Date.now() / 1000)
  try {
    const v = await loadView(db, planId, siteUrl, !!env.GCP_SA_KEY)
    if (!v) return
    const full = env as NotifyEnv & Env
    const queued = v.prefixes.filter(p => !v.deleted.has(p))
    const stage = ev && 'stage' in ev ? ev.stage : null
    const runEv = ev && 'run' in ev ? ev : null
    const by = stage?.by ?? (runEv?.phase === 'dispatched' ? runEv.run.actor : null)
    const [at, reg, people] = await Promise.all([
      stagedStats(full, queued).catch(e => { console.log(`staged slack sizing failed for plan ${planId}: ${(e as Error).message}`); return null }),
      loadRegistry(full).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>),
      slackPeople(env, [...v.stagers, ...v.runs.map(r => r.actor), ...(by ? [by] : [])]),
    ])
    const names: Names = Object.fromEntries(Object.entries(people).flatMap(([e, p]) => p.name ? [[e, p.name]] : []))
    const size = at ? sizeOf(at, queued, u => reg[u]?.name ?? u) : undefined
    const parent = renderParent({ ...v, names, size, image: await stagedCardUrl(env, db, siteUrl, v.digest) ?? undefined })
    const event: Event | undefined = runEv
      ? { text: runEvent(runEv.run, runEv.phase, { via: runEv.via, names, siteUrl }), ...(by ? { sender: personSender(by, people[by.toLowerCase()], 'dispatched') } : {}) }
      : stage ? undefined : (ev as Event | undefined)
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
      if (stage && queueWasEmpty(v.prefixes, new Set(stage.prefixes), v.deleted, stats)) ended = ':checkered_flag: Queue emptied: everything staged here is deleted (or was already empty).'
      else if (await parentDone(env, channel, ts)) ended = ':white_check_mark: Marked done.'
      if (ended) {
        const d = await detach(db, planId, ts)
        if (d.won) { old = ts; ts = null } else ts = d.channel === channel ? d.ts : null
      }
    }
    if (!ts) {
      const p = await slackApi(env, 'chat.postMessage', { channel, ...parent, ...PLAN_SENDER, unfurl_links: false })
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
        await slackApi(env, 'chat.update', { channel, ts, ...parent })
      }
    } else {
      await slackApi(env, 'chat.update', { channel, ts, ...parent })
    }
    if (old) {
      const pl = await slackApi(env, 'chat.getPermalink', { channel, message_ts: ts })
      const link = pl.ok && typeof pl.permalink === 'string' ? pl.permalink : null
      await slackApi(env, 'chat.postMessage', { channel, thread_ts: old, text: `${ended} Continued in ${link ? `<${link}|a new thread>` : 'a new thread'}.`, ...PLAN_SENDER, unfurl_links: false })
      await slackApi(env, 'chat.update', { channel, ts: old, ...closedParent(planId, siteUrl, link) })
    }
    if (stage) {
      await announceStage(env, db, {
        channel, thread: ts, batchId: stage.batchId, by: stage.by, now,
        ctx: { planId, siteUrl, names, stats: at?.stats ?? null },
        sender: personSender(stage.by, people[stage.by.toLowerCase()], 'staged'),
      })
    } else if (event) {
      await slackApi(env, 'chat.postMessage', { channel, thread_ts: ts, text: event.text, ...(event.blocks ? { blocks: event.blocks } : {}), ...(event.sender ?? PLAN_SENDER), unfurl_links: false })
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
    stagedStats(env as NotifyEnv & Env, items.map(i => i.prefix)).catch(() => null),
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

/** Announce runs that just finished (an executor's reflection) in their
 * plans' threads: the totals, or that the run ended without a result. */
export async function announceFinished(env: NotifyEnv, db: D1Database, runs: readonly FinishedRun[], siteUrl: string): Promise<void> {
  for (const { run_id, ok } of runs) {
    const r = await db.prepare('SELECT * FROM deletion_runs WHERE run_id = ?').bind(run_id).first<RunRow & { plan_id: number | null }>()
    if (r?.plan_id == null) continue
    // What the run deleted leaves the queue before the thread re-renders.
    await unstageDeleted(db, r.plan_id)
    await notifyPlan(env, db, r.plan_id, siteUrl, { run: r, phase: ok ? 'finished' : 'failed' })
  }
}

/** The plan's current real-deletion gate (fresh from D1), for `/slack/actions`. */
export async function planGate(db: D1Database, planId: number): Promise<{ gate: Gate; digest: string; items: number; closed: boolean } | null> {
  const v = await loadView(db, planId, '', false)
  if (!v) return null
  return { gate: realGate(v.runs, v.digest, v.items), digest: v.digest, items: v.items, closed: v.closed }
}
