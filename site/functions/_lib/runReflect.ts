/**
 * Reflect cw plan-first sweep runs into D1 (moved from api/plan-sweep/jobs.ts
 * so `/slack/actions` shares it; specs/done/staged-slack.md). The Batch job writes
 * only gs:// artifacts; a run's `<mode>-summary.json` fills in its
 * `deletion_runs` totals + `deletion_bands` (idempotent — only while
 * finished_ts IS NULL). A run is reflected as soon as its summary exists, even
 * while Batch still reports it RUNNING: the job's exit trap pings the site
 * then, so results land (and post to Slack) without anyone polling.
 */
import type { D1Database } from "@cloudflare/workers-types"
import type { BatchConfig } from "./batchConfig.js"
import { batchJobsUrl } from "./gcp.js"
import type { FinishedRun } from "./plans.js"

const HOLD_SECS = 7 * 24 * 3600 // undo window before purge (matches gcs's ≥7d soft-delete)
export const TERMINAL = new Set(["SUCCEEDED", "FAILED", "CANCELLED"])

export interface BatchJob {
  name: string
  uid: string
  createTime: string
  updateTime?: string
  status?: { state?: string; runDuration?: string; statusEvents?: { description?: string }[] }
  taskGroups?: { taskSpec?: { environment?: { variables?: Record<string, string> } } }[]
}

interface RunSummary {
  mode: string
  bucket?: string // the run's bucket (specs/done/cw-multi-bucket.md §4); older summaries: the primary
  deleted_objects: number
  deleted_bytes: number
  skipped_gone: number
  skipped_overwritten: number
  drift_new: number
  delete_failed: number
  finished_ts: number
  bands: { prefix: string; bytes: number; objects: number; gone: number; overwritten: number; drift_new: number }[]
}

async function readSummary(cfg: BatchConfig, token: string, jobId: string, mode: string): Promise<RunSummary | null> {
  const dir = mode === "real" ? "deleted" : "would-delete"
  const obj = encodeURIComponent(`sweep/cw/runs/${jobId}/${dir}-summary.json`)
  const r = await fetch(`https://storage.googleapis.com/storage/v1/b/${cfg.dataBucket}/o/${obj}?alt=media`, {
    headers: { authorization: `Bearer ${token}` },
  })
  if (!r.ok) return null
  return (await r.json().catch(() => null)) as RunSummary | null
}

async function reflect(db: D1Database, jobId: string, mode: string, s: RunSummary, primary: string): Promise<void> {
  const real = mode === "real"
  const undoDeadline = real ? s.finished_ts + HOLD_SECS : null
  const purgeState = real && s.deleted_objects > 0 ? "pending" : "none"
  await db.prepare(`
    UPDATE deletion_runs SET
      finished_ts = ?, deleted_bytes = ?, deleted_objects = ?, skipped_gone = ?,
      skipped_overwritten = ?, drift_dirs = ?, undo_deadline = ?, purge_state = ?
    WHERE run_id = ? AND finished_ts IS NULL
  `).bind(
    s.finished_ts, s.deleted_bytes, s.deleted_objects, s.skipped_gone,
    s.skipped_overwritten, s.drift_new, undoDeadline, purgeState, jobId,
  ).run()
  for (const b of s.bands ?? []) {
    await db.prepare(`
      INSERT OR REPLACE INTO deletion_bands
        (run_id, prefix, bytes, objects, gone, overwritten, drift_new_objects, undone_objects)
      VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    `).bind(jobId, `s3://${s.bucket ?? primary}/${b.prefix}`, b.bytes, b.objects, b.gone, b.overwritten, b.drift_new).run()
  }
}


/** The recent cw sweep / undo / purge Batch jobs (newest first). */
export async function listBatchJobs(cfg: BatchConfig, token: string): Promise<BatchJob[] | { error: string }> {
  const r = await fetch(`${batchJobsUrl(cfg, cfg.region)}?pageSize=100&orderBy=${encodeURIComponent("create_time desc")}`, {
    headers: { authorization: `Bearer ${token}` },
  })
  if (!r.ok) return { error: `batch list failed (${r.status})` }
  return ((await r.json()) as { jobs?: BatchJob[] }).jobs ?? []
}

export const sweepJobs = (jobs: BatchJob[]): BatchJob[] => jobs.filter(j => /\/jobs\/cw-sweep-(dry|real)-/.test(j.name)).slice(0, 20)
export const jobIdOf = (j: BatchJob): string => j.name.slice(j.name.lastIndexOf("/") + 1)

/** Reflect every in-progress run that has finished (summary present, or Batch
 * terminal without one) plus terminal undo/purge ops. Returns the runs this
 * call finished — the caller announces them. A run that ended without a
 * summary reviewed nothing: its `plan_digest` becomes the empty digest, so a
 * failed dry-run never opens the real gate. */
export async function reflectRuns(cfg: BatchConfig, db: D1Database, token: string, jobs: BatchJob[], primary: string): Promise<FinishedRun[]> {
  const done: FinishedRun[] = []
  const pending = new Set(
    (await db.prepare("SELECT run_id FROM deletion_runs WHERE finished_ts IS NULL").all<{ run_id: string }>())
      .results.map(x => x.run_id),
  )
  for (const j of sweepJobs(jobs)) {
    const jobId = jobIdOf(j)
    if (!pending.has(jobId)) continue
    const mode = jobId.startsWith("cw-sweep-real-") ? "real" : "dry"
    const terminal = TERMINAL.has(j.status?.state ?? "")
    const summary = await readSummary(cfg, token, jobId, mode)
    if (summary) {
      await reflect(db, jobId, mode, summary, primary)
      done.push({ run_id: jobId, ok: true })
    } else if (terminal) {
      const r = await db.prepare("UPDATE deletion_runs SET finished_ts = ?, plan_digest = '' WHERE run_id = ? AND finished_ts IS NULL")
        .bind(Math.floor(Date.now() / 1000), jobId).run()
      if (r.meta.changes) done.push({ run_id: jobId, ok: false })
    }
  }
  // Reflect terminal undo/purge ops onto their target run (state guards keep it
  // idempotent). TARGET_RUN + OP are in the op job's env.
  for (const j of jobs) {
    if (!/\/jobs\/cw-(undo|purge)-/.test(j.name) || j.status?.state !== "SUCCEEDED") continue
    const vars = j.taskGroups?.[0]?.taskSpec?.environment?.variables ?? {}
    const target = vars.TARGET_RUN
    if (!target) continue
    if (vars.OP === "undo") {
      await db.prepare("UPDATE deletion_runs SET undo_state = 'full' WHERE run_id = ? AND undo_state = 'partial'").bind(target).run()
    } else if (vars.OP === "purge") {
      await db.prepare("UPDATE deletion_runs SET purge_state = 'done' WHERE run_id = ? AND purge_state = 'pending'").bind(target).run()
    }
  }
  return done
}
