/**
 * The `laptop` executor (specs/m3-site.md Phase 3): the store is one laptop's
 * disk, which nothing on the edge can reach. A dispatch only records the run
 * in D1; the laptop's drainer (`disk-tree dispatch -s`) polls, sizes (dry) or
 * trashes (real) the plan's paths, and writes the totals back itself — so
 * `refresh` has nothing to close. Since a run that nobody drains would sit
 * "in progress" forever (and block the real gate), `prepare` refuses while
 * the drainer's heartbeat (`agents`, migration 0006) is stale: the items stay
 * staged and the console shows the reason.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type DispatchReq, type ExecEnv, type Executor, type Prepared, refuse } from './dispatch.js'
import type { FinishedRun } from './plans.js'

export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/
export const AGENT = 'drainer'
/** Seconds since the drainer's last check-in beyond which a dispatch is refused. */
export const STALE_S = 180

export async function agentSeen(db: D1Database, name: string = AGENT): Promise<number | null> {
  const r = await db.prepare('SELECT seen_ts FROM agents WHERE name = ?').bind(name).first<{ seen_ts: number }>()
  return r?.seen_ts ?? null
}

/** `20260929t221530z`: sortable, safe in a run id. */
const stamp = (now: number): string => new Date(now).toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'z').toLowerCase()

export function laptopExecutor(now: () => number = Date.now): Executor {
  return {
    dateRe: DATE_RE,
    dateHint: 'YYYY-MM-DD',
    async prepare(_env: ExecEnv, db: D1Database, req: DispatchReq): Promise<Prepared | ReturnType<typeof refuse>> {
      const plan = await db.prepare('SELECT id FROM plans WHERE id = ?').bind(req.planId).first<{ id: number }>()
      if (!plan) return refuse(404, 'no such plan')
      const prefixes = (await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ? ORDER BY prefix').bind(req.planId).all<{ prefix: string }>()).results.map(i => i.prefix)
      if (!prefixes.length) return refuse(400, 'plan has no items')
      const seen = await agentSeen(db)
      const age = seen == null ? null : Math.round(now() / 1000 - seen)
      if (age == null || age > STALE_S) {
        const when = age == null ? 'has never checked in' : `last checked in ${age} s ago`
        return refuse(503, `laptop not reachable: its drainer ${when}; the items stay staged`, { last_seen: seen })
      }
      const launch: Prepared['launch'] = async (date, digest) => {
        const runId = `laptop-${req.mode}-${stamp(now())}`
        // No manifest / log dir on the edge (the drainer keeps its trash
        // manifest on the laptop) and no ledger heads (plan-first).
        await db.prepare(`
          INSERT INTO deletion_runs (run_id, plan_id, manifest, scan, head, exec_head, actor, mode, started_ts, log_dir, plan_digest)
          VALUES (?, ?, 'laptop', ?, 0, 0, ?, ?, ?, 'laptop', ?)
        `).bind(runId, req.planId, date, req.actor, req.mode, Math.floor(now() / 1000), digest).run()
        return { job_id: runId, extra: { agent_seen: seen } }
      }
      return { prefixes, launch }
    },
    async refresh(): Promise<FinishedRun[]> {
      return []
    },
  }
}

export const laptop: Executor = laptopExecutor()
