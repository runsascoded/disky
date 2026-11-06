// POST /api/sweep/undo — undo a real run (remove its delete markers) on Batch.
// Body: { run_id }. Admin only. Allowed only while the run's undo window is open
// (mode real, undo_state != full, now < undo_deadline), and only for a run whose bucket retained something. Submits a Batch job
// running `plan-sweep undo` against the run dir. The run's `undo_state` moves only
// on what that job reports (`reflectRuns`), so a job that restores nothing changes nothing.
import type { D1Database } from "@cloudflare/workers-types"
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from "../../_lib/auth.js"
import { batchConfig, type BatchEnv, notConfigured } from "../../_lib/batchConfig.js"
import { jobStamp, runMountPath, submitBatch, sweepBatchSpec } from "../../_lib/cwBatch.js"
import { gcpToken } from "../../_lib/gcp.js"
import { auditRunControl, NO_SHAPE, prefixShape } from "../../_lib/plans.js"
import { runUndoNow } from "../../_lib/softDeleteNow.js"
import { runHasNoUndo, runNoUndoText, undoUnavailableText } from "../../../src/softDelete.js"

type Env = AuthEnv & BatchEnv & { DB?: D1Database; STORE_SCHEME?: string; STORE_BUCKETS?: string }

const RUN_RE = /^cw-sweep-(dry|real)-\d{8}-\d{6}z$/

interface RunRow {
  run_id: string
  mode: string
  undo_state: string
  undo_deadline: number | null
  min_soft_delete_days: number | null
  soft_delete_days: number | null
}

export const onRequestPost = async (ctx: Ctx & { env: Env }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.DB) return json({ error: "not configured (no D1 binding)" }, 503)
  if (!ctx.env.GCP_SA_KEY) return json({ error: "not configured (no GCP_SA_KEY)" }, 503)
  if (!ctx.env.JOB_SA) return json({ error: "not configured (no JOB_SA)" }, 503)
  const cfg = batchConfig(ctx.env, ["GCP_PROJECT", "DATA_BUCKET", "SWEEP_IMAGE", "SWEEP_S3_ENDPOINT"])
  if ("missing" in cfg) return json({ error: notConfigured("undo", cfg.missing) }, 503)
  const shape = prefixShape(ctx.env)
  if (!shape) return json({ error: `undo ${NO_SHAPE}` }, 503)
  const db = ctx.env.DB

  const runId = ((await ctx.request.json().catch(() => null)) as { run_id?: string } | null)?.run_id ?? ""
  if (!RUN_RE.test(runId)) return json({ error: "bad run_id" }, 400)
  const run = await db.prepare("SELECT run_id, mode, undo_state, undo_deadline, min_soft_delete_days, soft_delete_days FROM deletion_runs WHERE run_id = ?").bind(runId).first<RunRow>()
  if (!run) return json({ error: "no such run" }, 404)
  if (run.mode !== "real") return json({ error: "only real runs can be undone (a dry run deleted nothing)" }, 400)
  if (run.undo_state === "full") return json({ error: "already undone" }, 409)
  if (runHasNoUndo(run)) return json({ error: runNoUndoText(run) }, 409)
  if (run.undo_deadline && Math.floor(Date.now() / 1000) > run.undo_deadline) {
    return json({ error: "undo window expired (past the hold; the run may already be purged)" }, 409)
  }
  const { unavailable } = await runUndoNow(ctx.env, db, run, Math.floor(Date.now() / 1000))
  if (unavailable) return json({ error: undoUnavailableText(unavailable) }, 409)

  const jobId = `cw-undo-${jobStamp()}z`
  const script = `set -euo pipefail\ndt-cloud plan-sweep undo "${runMountPath(cfg, runId)}"`
  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  const { ok, status, text } = await submitBatch(cfg, token, jobId, sweepBatchSpec(cfg, ctx.env.JOB_SA, script, shape.buckets[0], { OP: "undo", TARGET_RUN: runId }, {}, "sweep-undo"))
  if (!ok) {
    console.error("undo submit failed", status, text.slice(0, 2000))
    return json({ error: `batch submit failed (${status})`, status }, 500)
  }
  await auditRunControl(db, "undo", runId, gated.email ?? "admin", jobId)
  return json({ job_id: jobId, target: runId, by: gated.email })
}
