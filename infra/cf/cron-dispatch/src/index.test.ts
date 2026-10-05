import { describe, expect, it } from 'vitest'
import { dispatchRequest, run, targetsFor } from './index'

const DISPATCH = JSON.stringify({
  '15 7 * * *': [{ repo: 'me/proj', workflow: 'daily.yml', ref: 'main' }, { repo: 'me/other', workflow: '42', ref: 'v1', inputs: { mode: 'full' } }],
  '0 * * * *': [{ repo: 'me/proj', workflow: 'health.yml', ref: 'main' }],
})

describe('targetsFor', () => {
  it("a cron expression's targets, by exact match", () => {
    expect(targetsFor('0 * * * *', DISPATCH)).toEqual([{ repo: 'me/proj', workflow: 'health.yml', ref: 'main' }])
  })
  it('an unwired expression is an error naming the known ones', () => {
    expect(() => targetsFor('5 7 * * *', DISPATCH)).toThrow('cron-dispatch: no DISPATCH entry for cron "5 7 * * *" (have "15 7 * * *", "0 * * * *")')
  })
})

describe('dispatchRequest', () => {
  it('POSTs workflow_dispatch with the ref and any inputs', async () => {
    const r = dispatchRequest({ repo: 'me/other', workflow: '42', ref: 'v1', inputs: { mode: 'full' } }, 'tok')
    expect([r.method, r.url, r.headers.get('authorization'), r.headers.get('x-github-api-version'), await r.text()]).toEqual([
      'POST', 'https://api.github.com/repos/me/other/actions/workflows/42/dispatches', 'Bearer tok', '2022-11-28', '{"ref":"v1","inputs":{"mode":"full"}}',
    ])
    expect(await dispatchRequest({ repo: 'me/proj', workflow: 'daily.yml', ref: 'main' }, 'tok').text()).toBe('{"ref":"main"}')
  })
})

describe('run', () => {
  const env = { DISPATCH, GITHUB_TOKEN: 'tok' }
  it('starts every target of the cron', async () => {
    const urls: string[] = []
    const ok = (async (req: Request) => { urls.push(req.url); return new Response(null, { status: 204 }) }) as typeof fetch
    expect(await run('15 7 * * *', env, ok)).toEqual(['me/proj daily.yml@main', 'me/other 42@v1'])
    expect(urls).toEqual([
      'https://api.github.com/repos/me/proj/actions/workflows/daily.yml/dispatches',
      'https://api.github.com/repos/me/other/actions/workflows/42/dispatches',
    ])
  })
  it("one target's failure still starts the rest, then fails the run with GitHub's reason", async () => {
    const urls: string[] = []
    const mixed = (async (req: Request) => {
      urls.push(req.url)
      return req.url.includes('/me/other/') ? new Response('{"message":"Bad credentials"}', { status: 401 }) : new Response(null, { status: 204 })
    }) as typeof fetch
    await expect(run('15 7 * * *', env, mixed)).rejects.toThrow('cron-dispatch: 1/2 failed — me/other 42@v1: 401 {"message":"Bad credentials"}')
    expect(urls.length).toBe(2)
  })
})
