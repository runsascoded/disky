/**
 * The staged-deletion review loop in Slack (specs/done/staged-slack.md): one thread
 * per staged plan in the deployment's admin channel. The parent message is
 * re-rendered on every event (counts, the latest dry-run, whether it still
 * matches the plan, the action buttons); each event is a reply. The pure parts
 * (`renderParent`, the event texts; the gate is `plans.realGate`) are what the
 * tests pin; the rest is Slack + D1 I/O, best-effort — a Slack failure never
 * fails the gesture that caused it.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type FinishedRun, type Gate, planDigest, planRuns, realGate, type RunRow } from './plans.js'
import { slackApi, slackReady, type SlackEnv } from './slack.js'
import type { Env } from './auth.js'
import { pathScans, storeReady } from './index.js'
import { prefixesAt } from './prefixes.js'
import { pathTree } from './pathTree.js'
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
  /** email → how to name them: a Slack mention (`<@U…>`) when the workspace
   *  knows the email, else absent (the email's local part). */
  mentions?: Record<string, string>
  /** The staged set's size at the latest scan. */
  size?: Sized
  /** The plan's share card (a signed, full-tier `/og/staged.png` URL). */
  image?: string
}

/** A staged set at one scan: totals and its largest owners (labels are
 *  Slack mentions where known). */
export interface Sized {
  scan: string
  b: number
  o: number
  /** Prefixes with nothing left at `scan`. */
  empty: number
  owners: { label: string; b: number }[]
}

const who = (email: string, mentions?: Record<string, string>): string => mentions?.[email.toLowerCase()] ?? email.replace(/@.*$/, '')

/** `51.0 TiB · 179,327,698 objects at scan 2026-10-02 · owners: <@U1> 16.0 TiB, Hedy 6.0 TiB`. */
export function sizeLine(z: Sized): string {
  const owners = z.owners.length ? ` · owners: ${z.owners.map(o => `${o.label} ${fmtBytes(o.b)}`).join(', ')}` : ''
  const empty = z.empty ? ` · ${fmtN(z.empty)} empty` : ''
  return `*${fmtBytes(z.b)}* · ${fmtN(z.o)} objects at scan ${z.scan}${empty}${owners}`
}

/** The thread's parent message: `{ text, blocks }` for chat.postMessage / chat.update. */
export function renderParent(v: ParentView): { text: string; blocks: unknown[] } {
  const title = v.closed
    ? `*Staged plan #${v.planId}* (closed)`
    : `*Staged for deletion* · plan #${v.planId} · ${fmtN(v.items)} ${v.items === 1 ? 'prefix' : 'prefixes'} in ${v.batches} ${v.batches === 1 ? 'batch' : 'batches'}`
  const by = (v.stagers.length ? `staged by ${v.stagers.map(e => who(e, v.mentions)).join(', ')}` : 'nothing staged')
    + (v.size ? `\n${sizeLine(v.size)}` : '')
  const gate = realGate(v.runs, v.digest, v.items)
  const latestDry = [...v.runs].filter(r => r.mode === 'dry').sort((a, b) => b.started_ts - a.started_ts)[0]
  let dryLine: string
  if (!latestDry) dryLine = 'No dry-run yet.'
  else if (latestDry.finished_ts == null) dryLine = `Dry-run running (${who(latestDry.actor, v.mentions)}, scan ${latestDry.scan}).`
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
      ? `\nReal run in progress (${who(lastReal.actor, v.mentions)}).`
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

/** A stage batch's reply, with its reject button. */
export function stageEvent(e: { planId: number; batchId: number; by: string; prefixes: string[]; covered: number; note: string | null; siteUrl: string; mentions?: Record<string, string>; size?: Sized }): { text: string; blocks: unknown[] } {
  // The prefixes as a `tree`: shared parents once, sibling leaves packed.
  const shown = '```' + pathTree(e.prefixes, { maxLines: 16 }).join('\n') + '```'
  const more = ''
  const cov = e.covered ? ` (${e.covered} already covered)` : ''
  const memo = e.note ? `\n> ${e.note.replace(/\n/g, '\n> ')}` : ''
  const text = `${who(e.by, e.mentions)} staged ${e.prefixes.length} ${e.prefixes.length === 1 ? 'prefix' : 'prefixes'}${cov}`
  const size = e.size ? `\n${sizeLine(e.size)}` : ''
  return {
    text,
    blocks: [
      { type: 'section', text: { type: 'mrkdwn', text: `:wastebasket: *${text}*${size}${memo}\n${shown}${more}` } },
      { type: 'actions', elements: [
        { type: 'button', action_id: 'staged_reject', value: `${e.planId}:${e.batchId}`, text: { type: 'plain_text', text: 'Reject batch' },
          confirm: { title: { type: 'plain_text', text: 'Reject this batch?' }, text: { type: 'mrkdwn', text: 'Unstages every prefix this batch added. Nothing is deleted.' }, confirm: { type: 'plain_text', text: 'Reject' }, deny: { type: 'plain_text', text: 'Cancel' } } },
        { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'View in www' }, url: `${e.siteUrl}/staged` },
      ] },
    ],
  }
}

/** A run's page on the site: `/staged?run=<id>` opens and scrolls to its row
 *  (the run id, or its Batch job's id before the executor records it). */
export const runUrl = (siteUrl: string, runId: string): string => `${siteUrl}/staged?run=${encodeURIComponent(runId)}`

/** A run's thread reply: the actor named by Slack mention where known, the run
 *  id linking to its row on `/staged`. */
export function runEvent(r: RunRow, phase: 'dispatched' | 'finished' | 'failed', o: { via?: string | null; mentions?: Record<string, string>; siteUrl?: string } = {}): string {
  const kind = r.mode === 'dry' ? 'Dry-run' : '*Real deletion*'
  const id = o.siteUrl ? `<${runUrl(o.siteUrl, r.run_id)}|${r.run_id}>` : `\`${r.run_id}\``
  if (phase === 'dispatched') return `${r.mode === 'dry' ? ':test_tube:' : ':rotating_light:'} ${kind} dispatched by ${who(r.actor, o.mentions)}${o.via ? ` via ${o.via}` : ''} on scan ${r.scan} (${id})`
  if (phase === 'failed') return `:x: ${kind} ${id} ended without a result (its Batch job stopped before the run summary); check its logs in www.`
  if (r.mode === 'dry') return `:test_tube: Dry-run ${id} finished: would delete *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects (gone since scan: ${fmtN(r.skipped_gone)}, overwritten: ${fmtN(r.skipped_overwritten)}).`
  return `:white_check_mark: Real deletion ${id} finished: deleted *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects${r.undo_deadline ? `; undoable until ${utc(r.undo_deadline)} (www)` : ''}.`
}

// ── I/O ────────────────────────────────────────────────────────────────────

/** `GCP_SA_KEY` = the deployment can dispatch (either executor), so the
 * parent message offers the dispatch buttons. */
export type NotifyEnv = SlackEnv & { GCP_SA_KEY?: string }

interface Event { text: string; blocks?: unknown[]; sender?: Sender }
/** The plan's full-tier card for the parent message, or null when the
 * deployment draws no cards (or has no `og_tokens` table). Slack fetches it
 * once per URL, so the view carries the plan's digest (`v`): a new batch is
 * a new image. Like any full card it's backed by an `og_tokens` row, minted
 * by `slack:staged` and reused while it has a week left, so revoking that row
 * on /admin reverts the thread's card on its next fetch. */
export async function stagedCardUrl(env: NotifyEnv & { OG_CARDS?: string; SESSION_SECRET?: string }, db: D1Database, siteUrl: string, digest: string, now = Math.floor(Date.now() / 1000)): Promise<string | null> {
  if (!env.OG_CARDS || !env.SESSION_SECRET || !siteUrl) return null
  const key = await ogKey(env.SESSION_SECRET)
  const tok = await serverToken(db, 'staged', {}, '/staged', 'slack:staged', now, IMAGE_TTL_DAYS).catch(() => null)
  if (!tok) return null
  return siteUrl + await imagePath(key, 'staged', imageParams({}, digest.slice(0, 8), { t: tok.token }), 'full', Math.min(expDay(now, IMAGE_TTL_DAYS), tok.day))
}

/** A stage batch's reply, rendered here so it can carry mentions and sizes. */
export interface StageArgs { stage: Omit<Parameters<typeof stageEvent>[0], 'mentions' | 'size'> }
/** A run's reply, rendered here so it can carry its actor's mention (and, for
 *  a dispatch, post as them). */
export interface RunArgs { run: RunRow; phase: 'dispatched' | 'finished' | 'failed'; via?: string | null }

/** A workspace member, as `users.lookupByEmail` returns them. */
export interface SlackPerson { mention: string; name: string | null; image: string | null }

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
        return u?.id && !u.deleted ? { mention: `<@${u.id}>`, name: u.profile?.real_name || u.real_name || null, image: u.profile?.image_192 ?? null } : null
      })().catch(() => null)
      personMemo.set(e, m)
    }
    const v = await m
    if (v) out[e] = v
  }))
  return out
}

/** Slack mentions for `emails`; unknown emails are left out (callers fall back
 *  to the local part). */
export async function slackMentions(env: SlackEnv, emails: readonly string[]): Promise<Record<string, string>> {
  const people = await slackPeople(env, emails)
  return Object.fromEntries(Object.entries(people).map(([e, p]) => [e, p.mention]))
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

/** A name as the site's canonical user id: `Grace Hopper` → `grace-hopper`. */
export const nameSlug = (name: string): string =>
  name.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '')

// The workspace's members as slug → `<@U…>`, per isolate (10 min).
let membersMemo: { at: number; p: Promise<Map<string, string>> } | null = null
/** The last `users.list` failure, for the refresh route's report. */
export let membersError: string | null = null

/** Workspace members keyed by their real and display names' slugs (`users.list`,
 *  `users:read`) — how an owner id with no known email still gets a mention.
 *  A slug two members share is dropped (no guessing). */
export function slackMembers(env: SlackEnv): Promise<Map<string, string>> {
  if (membersMemo && Date.now() - membersMemo.at < 10 * 60_000) return membersMemo.p
  const p = (async () => {
    const out = new Map<string, string>()
    const dup = new Set<string>()
    let cursor = ''
    for (let page = 0; page < 20; page++) {
      const r = await fetch(`https://slack.com/api/users.list?limit=500${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { headers: { authorization: `Bearer ${env.SLACK_BOT_TOKEN}` } })
      const j = (await r.json().catch(() => null)) as { ok?: boolean; members?: { id: string; deleted?: boolean; is_bot?: boolean; real_name?: string; profile?: { real_name?: string; display_name?: string } }[]; response_metadata?: { next_cursor?: string } } | null
      if (!j?.ok) { membersError = (j as { error?: string } | null)?.error ?? `http ${r.status}`; break }
      for (const m of j.members ?? []) {
        if (m.deleted || m.is_bot) continue
        const slugs = new Set([m.real_name, m.profile?.real_name, m.profile?.display_name].filter((x): x is string => !!x).map(nameSlug))
        for (const sl of slugs) {
          if (!sl) continue
          if (out.has(sl) && out.get(sl) !== `<@${m.id}>`) dup.add(sl)
          else out.set(sl, `<@${m.id}>`)
        }
      }
      cursor = j.response_metadata?.next_cursor ?? ''
      if (!cursor) break
    }
    for (const d of dup) out.delete(d)
    return out
  })().catch(() => new Map<string, string>())
  membersMemo = { at: Date.now(), p }
  return p
}

/** `prefixes` at the latest scan: totals and the top owners (by attributed
 *  bytes), owners named by Slack mention where their email is known. Null
 *  when the index isn't readable here. */
export async function sizeStaged(env: Env & SlackEnv, db: D1Database, prefixes: readonly string[], topOwners = 4): Promise<Sized | null> {
  if (!prefixes.length || !storeReady(env)) return null
  const scans = (await pathScans(env, true)).results
  const scan = scans[scans.length - 1]?.date
  if (!scan) return null
  const { stats } = await prefixesAt(env, scan, [...prefixes].slice(0, 1000))
  let b = 0, o = 0, empty = 0
  const byOwner = new Map<string, number>()
  for (const p of prefixes) {
    const st = stats[p]
    if (!st || !st.b) { empty++; continue }
    b += st.b; o += st.o
    for (const [u, ub] of st.us ?? []) byOwner.set(u, (byOwner.get(u) ?? 0) + ub)
  }
  const top = [...byOwner].sort((x, y) => y[1] - x[1]).slice(0, topOwners)
  const rows = top.length
    ? (await db.prepare(`SELECT email, user FROM user_emails WHERE user IN (${top.map(() => '?').join(',')})`).bind(...top.map(t => t[0])).all<{ email: string; user: string }>()).results
    : []
  const emailOf = new Map(rows.map(r => [r.user, r.email]))
  const [mentions, members, reg] = await Promise.all([slackMentions(env, [...emailOf.values()]), slackMembers(env), loadRegistry(env).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>)])
  // A mention when Slack knows them; else the site's display name.
  const owners = top.map(([u, ub]) => ({ label: mentions[emailOf.get(u)?.toLowerCase() ?? ''] ?? members.get(u) ?? reg[u]?.name ?? u, b: ub }))
  return { scan, b, o, empty, owners }
}

async function loadView(db: D1Database, planId: number, siteUrl: string, actions: boolean): Promise<(ParentView & { slack_ts: string | null; slack_channel: string | null }) | null> {
  const plan = await db.prepare('SELECT id, state, slack_ts, slack_channel FROM plans WHERE id = ?').bind(planId)
    .first<{ id: number; state: string; slack_ts: string | null; slack_channel: string | null }>()
  if (!plan) return null
  const items = (await db.prepare('SELECT prefix, added_by FROM plan_items WHERE plan_id = ?').bind(planId).all<{ prefix: string; added_by: string }>()).results
  const batches = await db.prepare('SELECT count(DISTINCT batch_id) AS n FROM plan_items WHERE plan_id = ? AND batch_id IS NOT NULL').bind(planId).first<{ n: number }>()
  const runs = await planRuns(db, planId)
  return {
    planId, siteUrl, actions, runs,
    items: items.length,
    batches: batches?.n ?? 0,
    stagers: [...new Set(items.map(i => i.added_by))].sort(),
    digest: await planDigest(items.map(i => i.prefix)),
    closed: plan.state !== 'open',
    slack_ts: plan.slack_ts,
    slack_channel: plan.slack_channel,
  }
}

/** Re-render the plan's parent message (posting it first if the plan has
 * none) and, with `event`, reply in its thread. Best-effort; never throws. */
export async function notifyPlan(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, ev?: Event | StageArgs | RunArgs): Promise<void> {
  if (!slackReady(env)) return
  try {
    const v = await loadView(db, planId, siteUrl, !!env.GCP_SA_KEY)
    if (!v) return
    const full = env as NotifyEnv & Env
    const items = (await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ?').bind(planId).all<{ prefix: string }>()).results.map(i => i.prefix)
    const stage = ev && 'stage' in ev ? ev.stage : null
    const runEv = ev && 'run' in ev ? ev : null
    const [mentions, size, stageSize] = await Promise.all([
      slackMentions(env, [...v.stagers, ...v.runs.map(r => r.actor), ...(stage ? [stage.by] : []), ...(runEv ? [runEv.run.actor] : [])]),
      sizeStaged(full, db, items).catch(() => null),
      stage ? sizeStaged(full, db, stage.prefixes).catch(() => null) : Promise.resolve(null),
    ])
    const parent = renderParent({ ...v, mentions, size: size ?? undefined, image: await stagedCardUrl(env, db, siteUrl, v.digest) ?? undefined })
    const by = stage?.by ?? (runEv?.phase === 'dispatched' ? runEv.run.actor : null)
    const people = by ? await slackPeople(env, [by]) : {}
    const event: Event | undefined = stage
      ? { ...stageEvent({ ...stage, mentions, size: stageSize ?? undefined }), sender: personSender(stage.by, people[stage.by.toLowerCase()], 'staged') }
      : runEv
        ? { text: runEvent(runEv.run, runEv.phase, { via: runEv.via, mentions, siteUrl }), ...(by ? { sender: personSender(by, people[by.toLowerCase()], 'dispatched') } : {}) }
        : (ev as Event | undefined)
    let channel = v.slack_channel ?? env.SLACK_ADMIN_CHANNEL!
    let ts = v.slack_ts
    if (!ts) {
      const p = await slackApi(env, 'chat.postMessage', { channel, ...parent, ...PLAN_SENDER, unfurl_links: false })
      if (!p.ok || !p.ts) return
      // Two concurrent first events would each post a parent: the claim is
      // the tiebreak, and the loser deletes its own.
      const claim = await db.prepare('UPDATE plans SET slack_channel = ?, slack_ts = ? WHERE id = ? AND slack_ts IS NULL').bind(p.channel ?? channel, p.ts, planId).run()
      if (claim.meta.changes) { ts = p.ts; channel = p.channel ?? channel }
      else {
        await slackApi(env, 'chat.delete', { channel: p.channel ?? channel, ts: p.ts })
        const row = await db.prepare('SELECT slack_channel, slack_ts FROM plans WHERE id = ?').bind(planId).first<{ slack_channel: string; slack_ts: string }>()
        if (!row?.slack_ts) return
        ts = row.slack_ts; channel = row.slack_channel
        await slackApi(env, 'chat.update', { channel, ts, ...parent })
      }
    } else {
      await slackApi(env, 'chat.update', { channel, ts, ...parent })
    }
    if (event) await slackApi(env, 'chat.postMessage', { channel, thread_ts: ts, text: event.text, ...(event.blocks ? { blocks: event.blocks } : {}), ...(event.sender ?? PLAN_SENDER), unfurl_links: false })
  } catch (e) {
    console.log(`staged slack notify failed for plan ${planId}: ${(e as Error).message}`)
  }
}

/** Re-render the plan's parent and the given stage-batch replies in place
 * (`chat.update`: no one is notified). Returns what was updated. */
export async function refreshThread(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, replies: Record<number, string>): Promise<{ parent: boolean; replies: Record<string, string> }> {
  const out = { parent: false, replies: {} as Record<string, string>, members: 0, membersError: null as string | null }
  if (!slackReady(env)) return out
  const plan = await db.prepare('SELECT slack_ts, slack_channel FROM plans WHERE id = ?').bind(planId).first<{ slack_ts: string | null; slack_channel: string | null }>()
  if (!plan?.slack_ts || !plan.slack_channel) return out
  await notifyPlan(env, db, planId, siteUrl)
  out.parent = true
  out.members = (await slackMembers(env)).size
  out.membersError = membersError
  const full = env as NotifyEnv & Env
  for (const [batch, ts] of Object.entries(replies)) {
    const b = await db.prepare('SELECT id, note, created_by FROM stage_batches WHERE id = ? AND plan_id = ?').bind(Number(batch), planId).first<{ id: number; note: string | null; created_by: string }>()
    if (!b) { out.replies[batch] = 'no such batch'; continue }
    const prefixes = (await db.prepare('SELECT prefix FROM plan_items WHERE batch_id = ? ORDER BY prefix').bind(b.id).all<{ prefix: string }>()).results.map(r => r.prefix)
    const [mentions, size] = await Promise.all([slackMentions(env, [b.created_by]), sizeStaged(full, db, prefixes).catch(() => null)])
    const msg = stageEvent({ planId, batchId: b.id, by: b.created_by, prefixes, covered: 0, note: b.note, siteUrl, mentions, size: size ?? undefined })
    const r = await slackApi(env, 'chat.update', { channel: plan.slack_channel, ts, ...msg })
    out.replies[batch] = r.ok ? 'updated' : (r.error ?? 'failed')
  }
  return out
}

/** Announce runs that just finished (an executor's reflection) in their
 * plans' threads: the totals, or that the run ended without a result. */
export async function announceFinished(env: NotifyEnv, db: D1Database, runs: readonly FinishedRun[], siteUrl: string): Promise<void> {
  for (const { run_id, ok } of runs) {
    const r = await db.prepare('SELECT * FROM deletion_runs WHERE run_id = ?').bind(run_id).first<RunRow & { plan_id: number | null }>()
    if (r?.plan_id != null) await notifyPlan(env, db, r.plan_id, siteUrl, { run: r, phase: ok ? 'finished' : 'failed' })
  }
}

/** The plan's current real-deletion gate (fresh from D1), for `/slack/actions`. */
export async function planGate(db: D1Database, planId: number): Promise<{ gate: Gate; digest: string; items: number; closed: boolean } | null> {
  const v = await loadView(db, planId, '', false)
  if (!v) return null
  return { gate: realGate(v.runs, v.digest, v.items), digest: v.digest, items: v.items, closed: v.closed }
}
