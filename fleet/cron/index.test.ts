/** The fleet clock's two jobs, against a stubbed fetch: exactly what each sends, and what it reports or throws. */
import { describe, expect, it } from 'vitest'
import { dispatchHealth, dispatchRequest, type Env, jobFor, refreshPost } from './index'

const ENV: Env = {
  FLEET_SLACK_URL: 'https://fleet.test/api/fleet/slack',
  FLEET_JOB_TOKEN: 'job-token',
  SLACK_CRON: '43 * * * *',
  HEALTH_CRON: '17 * * * *',
  HEALTH_WORKFLOW: 'Open-Athena/disky/health.yml@front-door',
  GH_DISPATCH_TOKEN: 'gh-token',
}

interface Call { url: string; method: string; headers: Record<string, string>; body: string | null }
const stub = (status: number, body: string) => {
  const calls: Call[] = []
  const f = (async (url: string, init: RequestInit) => {
    calls.push({ url, method: init.method!, headers: init.headers as Record<string, string>, body: (init.body as string) ?? null })
    return new Response(status === 204 ? null : body, { status })
  }) as unknown as typeof fetch
  return { f, calls }
}

describe('the fleet clock', () => {
  it('runs the job its cron names, and none for another', () => {
    expect([jobFor(ENV, '43 * * * *'), jobFor(ENV, '17 * * * *'), jobFor(ENV, '0 * * * *'), jobFor({ ...ENV, HEALTH_CRON: undefined }, '17 * * * *')])
      .toEqual(['slack', 'health', null, null])
  })

  it('refreshes the post with the job token and reports the route\'s answer, not its card URL', async () => {
    const { f, calls } = stub(200, JSON.stringify({ action: 'updated', channel: 'C1', ts: '1.2', image: 'https://fleet.test/og/fleet.png?sig=secret' }))
    expect(await refreshPost(ENV, f)).toEqual('fleet post updated C1 1.2')
    expect(calls).toEqual([{ url: 'https://fleet.test/api/fleet/slack', method: 'POST', headers: { authorization: 'Bearer job-token' }, body: null }])
  })

  it('a failed refresh throws with the status and the route\'s error', async () => {
    const { f } = stub(502, JSON.stringify({ error: 'chat.update: ratelimited' }))
    await expect(refreshPost(ENV, f)).rejects.toThrow('fleet post: HTTP 502: chat.update: ratelimited')
    await expect(refreshPost({ ...ENV, FLEET_JOB_TOKEN: undefined }, f)).rejects.toThrow('FLEET_JOB_TOKEN is not set')
  })

  it('dispatches the health workflow at its ref', async () => {
    const { f, calls } = stub(204, '')
    expect(await dispatchHealth(ENV, f)).toEqual('health dispatched (Open-Athena/disky/health.yml@front-door)')
    expect(calls).toEqual([{
      url: 'https://api.github.com/repos/Open-Athena/disky/actions/workflows/health.yml/dispatches',
      method: 'POST',
      headers: { authorization: 'Bearer gh-token', accept: 'application/vnd.github+json', 'x-github-api-version': '2022-11-28', 'user-agent': 'disky-fleet-cron' },
      body: '{"ref":"front-door"}',
    }])
  })

  it('skips the health dispatch until configured, and throws on GitHub\'s refusal', async () => {
    const { f, calls } = stub(403, 'Resource not accessible by personal access token')
    expect(await dispatchHealth({ ...ENV, GH_DISPATCH_TOKEN: undefined }, f)).toEqual('health: not configured (HEALTH_WORKFLOW / GH_DISPATCH_TOKEN unset)')
    expect(calls).toEqual([])
    await expect(dispatchHealth(ENV, f)).rejects.toThrow('health dispatch: HTTP 403: Resource not accessible by personal access token')
  })

  it('refuses a malformed workflow', () => {
    expect(dispatchRequest('a/b/c.yaml@main')).toEqual({ url: 'https://api.github.com/repos/a/b/actions/workflows/c.yaml/dispatches', ref: 'main' })
    expect(() => dispatchRequest('health.yml')).toThrow('HEALTH_WORKFLOW: want owner/repo/file.yml@ref, got "health.yml"')
  })
})
