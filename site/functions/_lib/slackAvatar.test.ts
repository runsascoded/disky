import { afterEach, describe, expect, it, vi } from 'vitest'
import { slackSenderOf } from './activity'
import { resetSlackPeople } from './slack'

afterEach(() => { vi.unstubAllGlobals(); resetSlackPeople() })

describe('Slack activity photos', () => {
  const env = { SLACK_BOT_TOKEN: 'test-token' }
  const fallback = { name: 'Ann', icon: 'https://site.example.test/a/site.png' }
  const actor = { actor: 'ann@example.test', via: 'session' }

  it('prefers the Slack photo while keeping the activity name', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true, user: { id: 'U1', profile: { real_name: 'Slack name', image_192: 'https://avatars.slack-edge.com/ann.png' } } }))))
    expect(await slackSenderOf(env, actor, fallback)).toEqual({ name: 'Ann', icon: 'https://avatars.slack-edge.com/ann.png' })
  })

  it.each([
    { ok: false, error: 'users_not_found' },
    { ok: false, error: 'missing_scope' },
    { ok: true, user: { id: 'U1', deleted: true, profile: { image_192: 'https://avatars.slack-edge.com/ann.png' } } },
    { ok: true, user: { id: 'U1', profile: { image_192: 'http://example.test/a.png' } } },
    { ok: true, user: { id: 'U1', profile: { image_192: 'invalid' } } },
  ])('keeps the fallback for an unavailable or unusable photo: %j', async response => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(response))))
    expect(await slackSenderOf(env, actor, fallback)).toEqual(fallback)
  })

  it('does not infer a Slack identity from a grant label', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    expect(await slackSenderOf(env, { actor: 'grant:ann@example.test', via: 'grant' }, fallback)).toEqual(fallback)
    expect(fetcher.mock.calls).toEqual([])
  })
})
