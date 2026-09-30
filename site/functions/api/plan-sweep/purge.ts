// POST /api/sweep/purge — permanently drop a real run's deleted versions on
// Batch (the irreversible space-reclaim stage). Body: { run_id }. Admin only.
// Allowed only after the undo hold has passed (now >= undo_deadline) and while
// purge_state = pending; refused once undone or done. /api/sweep/jobs reflects
// completion (purge_state -> done).
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
  purge_state: string
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
  const run = await db.prepare(
    "SELECT run_id, mode, undo_state, purge_state, undo_deadline FROM deletion_runs WHERE run_id = ?",
  ).bind(runId).first<RunRow>()
  if (!run) return json({ error: "no such run" }, 404)
  if (run.mode !== "real") return json({ error: "only real runs have versions to purge" }, 400)
  if (run.undo_state === "full") return json({ error: "run was undone; nothing to purge" }, 409)
  if (run.purge_state !== "pending") return json({ error: `not purgeable (purge_state=${run.purge_state})` }, 409)
  if (run.undo_deadline && Math.floor(Date.now() / 1000) < run.undo_deadline) {
    return json({ error: "still within the undo hold; purge is refused until the window closes" }, 409)
  }

  const jobId = `cw-purge-${jobStamp()}z`
  const script = `set -euo pipefail\ndt-cloud plan-sweep purge "${runMountPath(runId)}"`
  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  const { ok, status, text } = await submitBatch(token, jobId, sweepBatchSpec(script, { OP: "purge", TARGET_RUN: runId }))
  if (!ok) {
    console.error("purge submit failed", status, text.slice(0, 2000))
    return json({ error: `batch submit failed (${status})`, status }, 500)
  }
  return json({ job_id: jobId, target: runId, by: gated.email })
}
