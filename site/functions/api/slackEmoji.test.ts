// `/api/slack-emoji` (specs/done/digest-rewrite-review.md): the workspace's custom emoji, name → image URL, for
// `/admin/slack`'s `PlanView`: `emoji.list` with aliases resolved, cached per isolate, empty without a token or scope.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { EMOJI_TTL_MS, onRequestGet, resetSlackEmoji, workspaceEmoji } from './slack-emoji'

const ORIGIN = 'https://site.example.test'
const ARROW = 'https://emoji.slack-edge.com/T1/arrow_deg80/1.png'
const RUNWAY = 'https://emoji.slack-edge.com/T1/runway-3/2.png'
/** What `emoji.list` answers: two images, an alias of one, an alias of a stock emoji, a dangling alias and a cycle. */
const LISTED = {
  ok: true,
  emoji: {
    arrow_deg80: ARROW,
    'runway-3': RUNWAY,
    up80: 'alias:arrow_deg80',
    'up80-again': 'alias:up80',
    yes: 'alias:thumbsup',
    gone: 'alias:deleted-one',
    'loop-a': 'alias:loop-b',
    'loop-b': 'alias:loop-a',
  },
}
const RESOLVED = { arrow_deg80: ARROW, 'runway-3': RUNWAY, up80: ARROW, 'up80-again': ARROW }

let env: Record<string, unknown>
let admin: string
let viewer: string
/** What Slack's `emoji.list` answers next. */
let slack: unknown
let warn: ReturnType<typeof vi.spyOn>

beforeEach(async () => {
  resetSlackEmoji()
  const d = await sqliteD1()
  env = { DB: d.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', SLACK_BOT_TOKEN: 'xoxb-test' }
  const gate = gateFor(env as never)!
  const mint = (scopes: string[]) => gate.mint({ email: 'ops@example.test', scopes, name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })
  admin = (await mint(['cw', 'admin'])).token
  viewer = (await mint(['cw'])).token
  slack = LISTED
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

/** A fetch that answers Slack's `emoji.list` with `slack`, recording each call as `<method> <Slack method>`. */
function stubSlack(): () => string[] {
  const fetched = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input)
    void init
    return url.startsWith('https://slack.com/api/emoji.list') ? Response.json(slack) : new Response('not found', { status: 404 })
  })
  vi.stubGlobal('fetch', fetched)
  return () => fetched.mock.calls.map(([input, init]) => {
    const url = String(input instanceof Request ? input.url : input)
    return `${(input instanceof Request ? input.method : init?.method) ?? 'GET'} ${/^https:\/\/slack\.com\/api\/([\w.]+)/.exec(url)?.[1] ?? url}`
  })
}

/** The route's status and body, and the Slack calls it made, as `token` (none: signed out). */
async function call(token: string | null): Promise<[number, unknown, string[]]> {
  const calls = stubSlack()
  const r = await onRequestGet({
    request: new Request(`${ORIGIN}/api/slack-emoji`, { headers: token ? { authorization: `Bearer ${token}` } : {} }),
    env: env as never,
  } as never)
  return [r.status, await r.json(), calls()]
}

describe('/api/slack-emoji', () => {
  it('answers the workspace’s custom emoji, aliases resolved to their target’s image, reading Slack once per isolate', async () => {
    expect([await call(admin), await call(admin)]).toEqual([
      [200, { emoji: RESOLVED }, ['POST emoji.list']],
      [200, { emoji: RESOLVED }, []],
    ])
    expect(warn.mock.calls).toEqual([])
  })

  it('reads the list again once it is `EMOJI_TTL_MS` old', async () => {
    const calls = stubSlack()
    const t0 = 1_800_000_000_000
    expect([
      await workspaceEmoji('xoxb-test', t0),
      await workspaceEmoji('xoxb-test', t0 + EMOJI_TTL_MS - 1),
      await workspaceEmoji('xoxb-test', t0 + EMOJI_TTL_MS),
      calls(),
    ]).toEqual([RESOLVED, RESOLVED, RESOLVED, ['POST emoji.list', 'POST emoji.list']])
  })

  it('without a token, answers no emoji and says so once', async () => {
    delete env.SLACK_BOT_TOKEN
    expect([await call(admin), await call(admin)]).toEqual([[200, { emoji: {} }, []], [200, { emoji: {} }, []]])
    expect(warn.mock.calls).toEqual([['slack-emoji: no SLACK_BOT_TOKEN; custom emoji show as their names']])
  })

  it('with a token lacking `emoji:read`, answers no emoji and says so once per read', async () => {
    slack = { ok: false, error: 'missing_scope', needed: 'emoji:read', provided: 'chat:write' }
    expect([await call(admin), await call(admin)]).toEqual([[200, { emoji: {} }, ['POST emoji.list']], [200, { emoji: {} }, []]])
    expect(warn.mock.calls).toEqual([['slack-emoji: emoji.list failed (emoji.list: missing_scope); custom emoji show as their names']])
  })

  it('is for admins only: Slack isn’t asked for anyone else', async () => {
    expect([await call(null), await call(viewer)]).toEqual([
      [401, { error: 'unauthenticated' }, []],
      [403, { error: 'forbidden' }, []],
    ])
  })
})
