// `/api/digest-op` (specs/slack-preview-page.md phase 2): the job's OP through the site. Under the default params
// every parity fixture comes back exactly as the Python job rendered it (its `op` + `image`); a variant applies;
// bad requests are 400s; viewers only.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequestPost } from './digest-op'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string>; readdir(p: string): Promise<string[]> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const DIR = new URL('../../../cloud/tests/fixtures/digest/parity/', (import.meta as ImportMeta & { url: string }).url).pathname
type Fixture = { cfg: Record<string, unknown>; month: string; rows: Record<string, unknown>; plot_url: string | null; prev_url: string | null; op: string; image: unknown }
const fixture = async (name: string) => JSON.parse(await (await fsp()).readFile(DIR + name, 'utf8')) as Fixture

let env: Record<string, unknown>
let viewer: string

beforeAll(async () => {
  const { db } = await sqliteD1()
  env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs' }
  const gate = gateFor(env as never)!
  viewer = (await gate.mint({ email: 'job@example.test', scopes: ['gcs'], name: 'job', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
})

const post = async (body: unknown, token: string | null = viewer) => {
  const res = await onRequestPost({
    request: new Request('https://site.example.test/api/digest-op', {
      method: 'POST', body: JSON.stringify(body), headers: { 'content-type': 'application/json', ...(token ? { authorization: `Bearer ${token}` } : {}) },
    }),
    env,
  } as never)
  return [res.status, await res.json()] as const
}

const request = (f: Fixture, params?: string) => ({ month: f.month, rows: f.rows, cfg: f.cfg, params, plot_url: f.plot_url, prev_url: f.prev_url })

describe('/api/digest-op', () => {
  it("renders every parity fixture byte for byte as the Python job did, under the default params", async () => {
    const names = (await (await fsp()).readdir(DIR)).filter(n => n.endsWith('.json')).sort()
    expect(names.length).toBe(7)
    for (const n of names) {
      const f = await fixture(n)
      expect([n, ...(await post(request(f)))]).toEqual([n, 200, { text: f.op, image: f.image }])
      expect([n, ...(await post(request(f, '')))]).toEqual([n, 200, { text: f.op, image: f.image }])
    }
  })

  it('applies a variant: the quota line under the headline, the pools plot', async () => {
    const f = await fixture('cw-sep.json')
    const [status, body] = await post(request(f, 'qc,pp'))
    const lines = (body as { text: string }).text.split('\n')
    const live = f.op.split('\n')
    expect([status, lines[0], lines.slice(2), (body as { image: { kind: string } }).image.kind]).toEqual([
      200, live[0], live.slice(1), 'pools',
    ])
    expect(lines[1]).toBe('*Quotas:* :red_circle: hot 99.5% of 100Ti (0.5 Ti free) · :large_orange_circle: main 86.8% of 1P (120.0 Ti free)')
  })

  it('refuses a malformed request', async () => {
    const f = await fixture('cw-sep.json')
    const cases: [unknown, string][] = [
      [[], 'body: a JSON object'],
      [{ ...request(f), month: '2026-13' }, 'month: YYYY-MM'],
      [{ ...request(f), cfg: { ...f.cfg, template: 'hf' } }, "cfg.template: 'cw' | 'gcs'"],
      [{ ...request(f), rows: { ...f.rows, template: 'gcs' } }, "rows: the cw template's rows, at least one scan"],
      [{ ...request(f), rows: { ...f.rows, rows: [] } }, "rows: the cw template's rows, at least one scan"],
      [{ ...request(f), plot_url: 3 }, 'params, plot_url, prev_url: strings'],
    ]
    for (const [body, error] of cases) expect(await post(body)).toEqual([400, { error }])
    expect(await post(request(f, 'w30,zz'))).toEqual([400, { error: 'params: digest params: unreadable field "zz" in "w30,zz"' }])
    expect(await post(request(f, 'w30,w45'))).toEqual([400, { error: 'params: digest params: repeated field "w" in "w30,w45"' }])
  })

  it('is for viewers only', async () => {
    const f = await fixture('cw-sep.json')
    expect((await post(request(f), null))[0]).toBe(401)
  })
})
