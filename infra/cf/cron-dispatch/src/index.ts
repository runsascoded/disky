/**
 * cron-dispatch: start GitHub Actions workflows on Cloudflare's cron.
 *
 * GitHub's `schedule:` trigger is best-effort: this repo's 07:15 UTC daily ran
 * 5–8 h late, every day. Cloudflare's cron triggers fire on the minute, and a
 * `workflow_dispatch` starts its run within seconds, so the workflow keeps
 * running on GitHub's (free, for public repos) runners and only the clock moves.
 *
 * Configuration only, no code per project (`wrangler.example.toml`): the
 * schedules are `[triggers] crons`, and `DISPATCH` maps each cron expression
 * to the workflows it starts. The token is the `GITHUB_TOKEN` secret: a
 * fine-grained token with Actions read/write on the named repos, nothing else.
 */

export interface Target {
  /** `owner/name` */
  repo: string
  /** The workflow's file name (`daily-ingest.yml`) or numeric id. */
  workflow: string
  /** The branch or tag to run on. */
  ref: string
  /** `workflow_dispatch` inputs, if the workflow declares any. */
  inputs?: Record<string, string>
}

export interface Env {
  /** JSON: `{"<cron expression>": Target[]}`; keys match `[triggers] crons` exactly. */
  DISPATCH: string
  GITHUB_TOKEN: string
}

/** The targets a cron expression starts. An expression with no entry is a
 *  config error (a trigger nobody wired up), not a silent no-op. */
export function targetsFor(cron: string, dispatch: string): Target[] {
  const map = JSON.parse(dispatch) as Record<string, Target[]>
  const ts = map[cron]
  if (!ts) throw new Error(`cron-dispatch: no DISPATCH entry for cron "${cron}" (have ${Object.keys(map).map(k => `"${k}"`).join(', ')})`)
  return ts
}

/** The GitHub API request that starts `t`. */
export function dispatchRequest(t: Target, token: string): Request {
  return new Request(`https://api.github.com/repos/${t.repo}/actions/workflows/${encodeURIComponent(t.workflow)}/dispatches`, {
    method: 'POST',
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${token}`,
      'content-type': 'application/json',
      'user-agent': 'cron-dispatch',
      'x-github-api-version': '2022-11-28',
    },
    body: JSON.stringify({ ref: t.ref, ...(t.inputs ? { inputs: t.inputs } : {}) }),
  })
}

/** Start every target of `cron`; one target's failure doesn't stop the others,
 *  but any failure fails the invocation (Cloudflare records it). */
export async function run(cron: string, env: Env, fetchImpl: typeof fetch = fetch): Promise<string[]> {
  const targets = targetsFor(cron, env.DISPATCH)
  const results = await Promise.all(targets.map(async t => {
    const r = await fetchImpl(dispatchRequest(t, env.GITHUB_TOKEN))
    // 204 No Content is success; anything else carries GitHub's reason.
    return r.status === 204 ? null : `${t.repo} ${t.workflow}@${t.ref}: ${r.status} ${(await r.text()).slice(0, 200)}`
  }))
  const failed = results.filter((x): x is string => x !== null)
  for (const t of targets) console.log(`cron-dispatch ${cron}: ${t.repo} ${t.workflow}@${t.ref}`)
  if (failed.length) throw new Error(`cron-dispatch: ${failed.length}/${targets.length} failed — ${failed.join('; ')}`)
  return targets.map(t => `${t.repo} ${t.workflow}@${t.ref}`)
}

export default {
  // Awaited, not `waitUntil`: a failed dispatch fails the invocation, which
  // the dashboard's cron history records.
  async scheduled(event: ScheduledController, env: Env): Promise<void> {
    await run(event.cron, env)
  },
}
