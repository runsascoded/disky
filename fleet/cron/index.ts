/**
 * The fleet's clock (specs/disky-oa-dev.md): a Worker whose Cron Triggers do what GitHub's `schedule:` did less
 * reliably (late or dropped runs under load, and schedules disabled after 60 days without repo activity).
 *
 * - `SLACK_CRON`: POST the fleet site's Slack route (`FLEET_SLACK_URL`, Bearer `FLEET_JOB_TOKEN`), which reads the
 *   deployments and edits the kept post. The work stays in the fleet app; this is only its clock.
 * - `HEALTH_CRON`: dispatch the health workflow (`HEALTH_WORKFLOW`, `owner/repo/file@ref`) through GitHub's API with
 *   `GH_DISPATCH_TOKEN`. A dispatch isn't dropped or disabled the way a `schedule:` is; the checks still run on
 *   Actions, each deployment's own `dt-cloud`. Off while either is unset.
 *
 * A failed job throws, so the Workers dashboard marks that cron run failed.
 */

export interface Env {
  FLEET_SLACK_URL: string
  FLEET_JOB_TOKEN?: string
  SLACK_CRON: string
  HEALTH_CRON?: string
  HEALTH_WORKFLOW?: string
  GH_DISPATCH_TOKEN?: string
}

type Fetch = typeof fetch

/** The fleet post's refresh: the route's answer (`{ action, channel, ts }`), never the signed card URL it also returns. */
export async function refreshPost(env: Env, f: Fetch = fetch): Promise<string> {
  if (!env.FLEET_JOB_TOKEN) throw new Error('FLEET_JOB_TOKEN is not set')
  const r = await f(env.FLEET_SLACK_URL, { method: 'POST', headers: { authorization: `Bearer ${env.FLEET_JOB_TOKEN}` } })
  const body = await r.json().catch(() => null) as { action?: string; channel?: string; ts?: string; error?: string } | null
  if (!r.ok) throw new Error(`fleet post: HTTP ${r.status}${body?.error ? `: ${body.error}` : ''}`)
  return `fleet post ${body?.action} ${body?.channel} ${body?.ts}`
}

/** `owner/repo/file@ref` → the workflow-dispatch request. */
export function dispatchRequest(workflow: string): { url: string; ref: string } {
  const m = /^([\w.-]+)\/([\w.-]+)\/([\w.-]+\.ya?ml)@(.+)$/.exec(workflow)
  if (!m) throw new Error(`HEALTH_WORKFLOW: want owner/repo/file.yml@ref, got ${JSON.stringify(workflow)}`)
  const [, owner, repo, file, ref] = m
  return { url: `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${file}/dispatches`, ref }
}

/** The health checks' dispatch: GitHub answers 204 with no body. */
export async function dispatchHealth(env: Env, f: Fetch = fetch): Promise<string> {
  if (!env.HEALTH_WORKFLOW || !env.GH_DISPATCH_TOKEN) return 'health: not configured (HEALTH_WORKFLOW / GH_DISPATCH_TOKEN unset)'
  const { url, ref } = dispatchRequest(env.HEALTH_WORKFLOW)
  const r = await f(url, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
      accept: 'application/vnd.github+json',
      'x-github-api-version': '2022-11-28',
      'user-agent': 'disky-fleet-cron',
    },
    body: JSON.stringify({ ref }),
  })
  if (r.status !== 204) throw new Error(`health dispatch: HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`)
  return `health dispatched (${env.HEALTH_WORKFLOW})`
}

/** Which job a cron expression runs. */
export function jobFor(env: Env, cron: string): 'slack' | 'health' | null {
  if (cron === env.SLACK_CRON) return 'slack'
  if (env.HEALTH_CRON && cron === env.HEALTH_CRON) return 'health'
  return null
}

export default {
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    const job = jobFor(env, controller.cron)
    if (!job) throw new Error(`no job for cron ${JSON.stringify(controller.cron)}`)
    console.log(job === 'slack' ? await refreshPost(env) : await dispatchHealth(env))
  },
}
