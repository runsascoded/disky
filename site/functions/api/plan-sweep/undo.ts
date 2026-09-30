// POST /api/sweep/undo — undo a real run (remove its delete markers) on Batch.
// Body: { run_id }. Admin only. Allowed only while the run's undo window is open
// (mode real, undo_state != full, now < undo_deadline). Submits a Batch job
// running `sweep undo` against the run dir; /api/sweep/jobs reflects completion
// (undo_state -> full).
import type { D1Database } from "@cloudflare/workers-types"
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from "../../_lib/auth.js"
import { gcpToken } from "../../_lib/gcp.js"
import { jobStamp, runMountPath, submitBatch, sweepBatchSpec } from "../../_lib/cwBatch.js"

type Env = AuthEnv & { DB?: D1Database }

const RUN_RE = /^cw-sweep-(dry|real)-\d{8}-\d{6}z$/

interface RunRow {
  run_id: string
  mode: string
  undo_state: string
  undo_deadline: number | null
}

export const onRequestPost = async (ctx: Ctx & { env: Env }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: "not configured (no D1 binding)" }, 503)
  if (!ctx.env.GCP_SA_KEY) return json({ error: "not configured (no GCP_SA_KEY)" }, 503)
  const db = ctx.env.DB

  const runId = ((await ctx.request.json().catch(() => null)) as { run_id?: string } | null)?.run_id ?? ""
  if (!RUN_RE.test(runId)) return json({ error: "bad run_id" }, 400)
  const run = await db.prepare("SELECT run_id, mode, undo_state, undo_deadline FROM deletion_runs WHERE run_id = ?").bind(runId).first<RunRow>()
  if (!run) return json({ error: "no such run" }, 404)
  if (run.mode !== "real") return json({ error: "only real runs can be undone (a dry run deleted nothing)" }, 400)
  if (run.undo_state === "full") return json({ error: "already undone" }, 409)
  if (run.undo_deadline && Math.floor(Date.now() / 1000) > run.undo_deadline) {
    return json({ error: "undo window expired (past the hold; the run may already be purged)" }, 409)
  }

  const jobId = `cw-undo-${jobStamp()}z`
  const script = `set -euo pipefail\ndt-cloud plan-sweep undo "${runMountPath(runId)}"`
  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  const { ok, status, text } = await submitBatch(token, jobId, sweepBatchSpec(script, { OP: "undo", TARGET_RUN: runId }))
  if (!ok) {
    console.error("undo submit failed", status, text.slice(0, 2000))
    return json({ error: `batch submit failed (${status})`, status }, 500)
  }
  await db.prepare("UPDATE deletion_runs SET undo_state = 'partial' WHERE run_id = ?").bind(runId).run()
  return json({ job_id: jobId, target: runId, by: gated.email })
}
