// GET /api/plan-sweep/jobs — recent `cw-sweep-*` Batch jobs with live state, so
// the console shows a dispatch from the moment it's submitted. Any
// authenticated viewer may read; the payload holds no bucket data. Reading
// also reflects finished runs into D1 (`_lib/runReflect.ts`) and posts each
// newly finished run's result to its plan's Slack thread
// (specs/done/staged-slack.md) — the Batch job's exit trap calls this with the
// job's read grant, so that happens as the run ends.
import type { D1Database } from "@cloudflare/workers-types"
import { type Ctx, type Env as AuthEnv, json, requireViewer } from "../../_lib/auth.js"
import { GCP_PROJECT, BATCH_REGION, gcpToken } from "../../_lib/gcp.js"
import { DATA_BUCKET } from "../../_lib/cwBatch.js"
import { jobIdOf, listBatchJobs, reflectRuns, sweepJobs } from "../../_lib/runReflect.js"
import { announceFinished, type NotifyEnv } from "../../_lib/stagedSlack.js"

type Env = AuthEnv & NotifyEnv & { DB?: D1Database }

export const onRequestGet = async (ctx: Ctx & { env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ jobs: [], configured: false })
  const token = await gcpToken(ctx.env.GCP_SA_KEY)

  const jobs = await listBatchJobs(token)
  if (!Array.isArray(jobs)) {
    console.error(jobs.error)
    return json({ error: jobs.error }, 500)
  }
  const db = ctx.env.DB
  if (db) {
    const finished = await reflectRuns(db, token, jobs)
    if (finished.length) {
      const p = announceFinished(ctx.env, db, finished, new URL(ctx.request.url).origin)
      if (ctx.waitUntil) ctx.waitUntil(p)
      else await p
    }
  }

  const out = sweepJobs(jobs).map(j => {
    const jobId = jobIdOf(j)
    const vars = j.taskGroups?.[0]?.taskSpec?.environment?.variables ?? {}
    const ev = j.status?.statusEvents ?? []
    const dur = j.status?.runDuration
    return {
      job_id: jobId,
      mode: jobId.startsWith("cw-sweep-real-") ? "real" : "dry",
      state: j.status?.state ?? "UNKNOWN",
      created: j.createTime,
      updated: j.updateTime ?? null,
      run_secs: dur ? Number(dur.replace(/s$/, "")) : null,
      date: vars.SWEEP_DATE ?? null,
      run: `gs://${DATA_BUCKET}/sweep/cw/runs/${jobId}`,
      last_event: ev.length ? ev[ev.length - 1].description ?? null : null,
      logs: `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(`labels.job_uid="${j.uid}"`)}?project=${GCP_PROJECT}`,
    }
  })
  return json({ jobs: out, configured: true, region: BATCH_REGION }, 200, { "cache-control": "private, max-age=10" })
}
