// GET /api/plan-sweep/jobs — recent `cw-sweep-*` Batch jobs with live state, so
// the console shows a dispatch from the moment it's submitted. Any
// authenticated viewer may read; the payload holds no bucket data. Reading
// also reflects finished runs into D1 (`_lib/runReflect.ts`) and posts each
// newly finished run's result to its plan's Slack thread
// (specs/done/staged-slack.md) — the Batch job's exit trap calls this with the
// job's read grant, so that happens as the run ends.
import type { D1Database } from "@cloudflare/workers-types"
import { type Ctx, type Env as AuthEnv, json, requireViewer } from "../../_lib/auth.js"
import { batchConfig, type BatchEnv, notConfigured } from "../../_lib/batchConfig.js"
import { runGsPath } from "../../_lib/cwBatch.js"
import { gcpToken } from "../../_lib/gcp.js"
import { NO_SHAPE, prefixShape } from "../../_lib/plans.js"
import { jobIdOf, listBatchJobs, reflectRuns, sweepJobs } from "../../_lib/runReflect.js"
import { announceFinished, type NotifyEnv } from "../../_lib/stagedSlack.js"

type Env = AuthEnv & NotifyEnv & BatchEnv & { DB?: D1Database; STORE_SCHEME?: string; STORE_BUCKETS?: string }

export const onRequestGet = async (ctx: Ctx & { env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ jobs: [], configured: false })
  const cfg = batchConfig(ctx.env, ["GCP_PROJECT", "DATA_BUCKET"])
  if ("missing" in cfg) return json({ error: notConfigured("jobs", cfg.missing) }, 503)
  const shape = prefixShape(ctx.env)
  if (!shape) return json({ error: `jobs ${NO_SHAPE}` }, 503)
  const token = await gcpToken(ctx.env.GCP_SA_KEY)

  const jobs = await listBatchJobs(cfg, token)
  if (!Array.isArray(jobs)) {
    console.error(jobs.error)
    return json({ error: jobs.error }, 500)
  }
  const db = ctx.env.DB
  if (db) {
    const finished = await reflectRuns(cfg, db, token, jobs, shape.buckets[0])
    if (finished.length) {
      const p = announceFinished(ctx.env, db, finished, new URL(ctx.request.url).origin)
      if (ctx.waitUntil) ctx.waitUntil(p)
      else await p
    }
  }

  // The sweep runs, then the undo / purge ops on them (`op`, `target` = the
  // run id), which /staged shows on their run's row while they're live.
  const ops = jobs.filter(j => /\/jobs\/cw-(undo|purge)-/.test(j.name)).slice(0, 20)
  const out = [...sweepJobs(jobs), ...ops].map(j => {
    const jobId = jobIdOf(j)
    const vars = j.taskGroups?.[0]?.taskSpec?.environment?.variables ?? {}
    const ev = j.status?.statusEvents ?? []
    const dur = j.status?.runDuration
    const op = vars.OP === "undo" || vars.OP === "purge" ? vars.OP : "sweep"
    return {
      job_id: jobId,
      op,
      target: op === "sweep" ? null : vars.TARGET_RUN ?? null,
      plan_id: null,
      mode: op !== "sweep" || jobId.startsWith("cw-sweep-real-") ? "real" : "dry",
      state: j.status?.state ?? "UNKNOWN",
      created: j.createTime,
      updated: j.updateTime ?? null,
      run_secs: dur ? Number(dur.replace(/s$/, "")) : null,
      date: vars.SWEEP_DATE ?? null,
      run: runGsPath(cfg, jobId),
      last_event: ev.length ? ev[ev.length - 1].description ?? null : null,
      logs: `https://console.cloud.google.com/logs/query;query=${encodeURIComponent(`labels.job_uid="${j.uid}"`)}?project=${cfg.project}`,
    }
  })
  return json({ jobs: out, configured: true, region: cfg.region }, 200, { "cache-control": "private, max-age=10" })
}
