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
}

const who = (email: string): string => email.replace(/@.*$/, '')

/** The thread's parent message: `{ text, blocks }` for chat.postMessage / chat.update. */
export function renderParent(v: ParentView): { text: string; blocks: unknown[] } {
  const title = v.closed
    ? `*Staged plan #${v.planId}* (closed)`
    : `*Staged for deletion* · plan #${v.planId} · ${fmtN(v.items)} ${v.items === 1 ? 'prefix' : 'prefixes'} in ${v.batches} ${v.batches === 1 ? 'batch' : 'batches'}`
  const by = v.stagers.length ? `staged by ${v.stagers.map(who).join(', ')}` : 'nothing staged'
  const gate = realGate(v.runs, v.digest, v.items)
  const latestDry = [...v.runs].filter(r => r.mode === 'dry').sort((a, b) => b.started_ts - a.started_ts)[0]
  let dryLine: string
  if (!latestDry) dryLine = 'No dry-run yet.'
  else if (latestDry.finished_ts == null) dryLine = `Dry-run running (${who(latestDry.actor)}, scan ${latestDry.scan}).`
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
      ? `\nReal run in progress (${who(lastReal.actor)}).`
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
  ]
  return { text: `Staged for deletion: plan #${v.planId}, ${v.items} prefixes`, blocks }
}

/** A stage batch's reply, with its reject button. */
export function stageEvent(e: { planId: number; batchId: number; by: string; prefixes: string[]; covered: number; note: string | null; siteUrl: string }): { text: string; blocks: unknown[] } {
  const shown = e.prefixes.slice(0, 8).map(p => `• \`${p}\``).join('\n')
  const more = e.prefixes.length > 8 ? `\n…and ${e.prefixes.length - 8} more` : ''
  const cov = e.covered ? ` (${e.covered} already covered)` : ''
  const memo = e.note ? `\n> ${e.note.replace(/\n/g, '\n> ')}` : ''
  const text = `${who(e.by)} staged ${e.prefixes.length} ${e.prefixes.length === 1 ? 'prefix' : 'prefixes'}${cov}`
  return {
    text,
    blocks: [
      { type: 'section', text: { type: 'mrkdwn', text: `:wastebasket: *${text}*${memo}\n${shown}${more}` } },
      { type: 'actions', elements: [
        { type: 'button', action_id: 'staged_reject', value: `${e.planId}:${e.batchId}`, text: { type: 'plain_text', text: 'Reject batch' },
          confirm: { title: { type: 'plain_text', text: 'Reject this batch?' }, text: { type: 'mrkdwn', text: 'Unstages every prefix this batch added. Nothing is deleted.' }, confirm: { type: 'plain_text', text: 'Reject' }, deny: { type: 'plain_text', text: 'Cancel' } } },
        { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'View in www' }, url: `${e.siteUrl}/staged` },
      ] },
    ],
  }
}

export function runEvent(r: RunRow, phase: 'dispatched' | 'finished' | 'failed', via: string | null = null): string {
  const kind = r.mode === 'dry' ? 'Dry-run' : '*Real deletion*'
  if (phase === 'dispatched') return `${r.mode === 'dry' ? ':test_tube:' : ':rotating_light:'} ${kind} dispatched by ${who(r.actor)}${via ? ` via ${via}` : ''} on scan ${r.scan} (\`${r.run_id}\`)`
  if (phase === 'failed') return `:x: ${kind} \`${r.run_id}\` ended without a result (its Batch job stopped before the run summary); check its logs in www.`
  if (r.mode === 'dry') return `:test_tube: Dry-run finished: would delete *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects (gone since scan: ${fmtN(r.skipped_gone)}, overwritten: ${fmtN(r.skipped_overwritten)}).`
  return `:white_check_mark: Real deletion finished: deleted *${fmtBytes(r.deleted_bytes)}* / ${fmtN(r.deleted_objects)} objects${r.undo_deadline ? `; undoable until ${utc(r.undo_deadline)} (www)` : ''}.`
}

// ── I/O ────────────────────────────────────────────────────────────────────

/** `GCP_SA_KEY` = the deployment can dispatch (either executor), so the
 * parent message offers the dispatch buttons. */
export type NotifyEnv = SlackEnv & { GCP_SA_KEY?: string }

interface Event { text: string; blocks?: unknown[] }

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
export async function notifyPlan(env: NotifyEnv, db: D1Database, planId: number, siteUrl: string, event?: Event): Promise<void> {
  if (!slackReady(env)) return
  try {
    const v = await loadView(db, planId, siteUrl, !!env.GCP_SA_KEY)
    if (!v) return
    const parent = renderParent(v)
    let channel = v.slack_channel ?? env.SLACK_ADMIN_CHANNEL!
    let ts = v.slack_ts
    if (!ts) {
      const p = await slackApi(env, 'chat.postMessage', { channel, ...parent, unfurl_links: false })
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
    if (event) await slackApi(env, 'chat.postMessage', { channel, thread_ts: ts, text: event.text, ...(event.blocks ? { blocks: event.blocks } : {}), unfurl_links: false })
  } catch (e) {
    console.log(`staged slack notify failed for plan ${planId}: ${(e as Error).message}`)
  }
}

/** Announce runs that just finished (an executor's reflection) in their
 * plans' threads: the totals, or that the run ended without a result. */
export async function announceFinished(env: NotifyEnv, db: D1Database, runs: readonly FinishedRun[], siteUrl: string): Promise<void> {
  for (const { run_id, ok } of runs) {
    const r = await db.prepare('SELECT * FROM deletion_runs WHERE run_id = ?').bind(run_id).first<RunRow & { plan_id: number | null }>()
    if (r?.plan_id != null) await notifyPlan(env, db, r.plan_id, siteUrl, { text: runEvent(r, ok ? 'finished' : 'failed') })
  }
}

/** The plan's current real-deletion gate (fresh from D1), for `/slack/actions`. */
export async function planGate(db: D1Database, planId: number): Promise<{ gate: Gate; digest: string; items: number; closed: boolean } | null> {
  const v = await loadView(db, planId, '', false)
  if (!v) return null
  return { gate: realGate(v.runs, v.digest, v.items), digest: v.digest, items: v.items, closed: v.closed }
}
