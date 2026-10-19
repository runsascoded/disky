// `/api/annotations` (spec `chart-annotations.md`): viewers read a store's notes, admins write them (each write
// audited in `admin_edits`), a secondary store's notes are its own and need its scope, and a D1 without the table
// reads as an honest empty list — over the D1 schema and the real gate (Bearer grants minted in the same D1).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NOT_APPLIED, SCOPE_NOT_APPLIED, onRequest } from './annotations'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { parseNote } from '../_lib/annotationShape'

const NOW = Date.UTC(2026, 9, 18, 12, 0, 0)
const T = NOW / 1000
const HOST = 'https://site.example.test'
const STORES_JSON = JSON.stringify({ meta: { scope: 'admin', vars: { ROOT_LABEL: 'our storage' } } })

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW) })
afterEach(() => { vi.useRealTimers() })

async function setup() {
  const { db, raw } = await sqliteD1()
  const env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', STORES_JSON }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  const viewer = await tok('ann@example.test', ['cw'])
  const admin = await tok('ops@example.test', ['cw', 'admin'])
  const call = async (method: string, query = '', opts: { token?: string; body?: unknown } = {}) => {
    const request = new Request(`${HOST}/api/annotations${query}`, {
      method,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      headers: opts.token ? { authorization: `Bearer ${opts.token}` } : {},
    })
    const res = await onRequest({ request, env } as never)
    return [res.status, await res.json()] as const
  }
  const edits = () => (raw.prepare("SELECT tbl, pk, action, who, ts, old_json, new_json FROM admin_edits WHERE tbl = 'chart_annotations' ORDER BY id").all() as { tbl: string; pk: string; action: string; who: string; ts: number; old_json: string | null; new_json: string | null }[])
    .map(r => ({ ...r, old_json: r.old_json ? JSON.parse(r.old_json as string) : null, new_json: r.new_json ? JSON.parse(r.new_json as string) : null }))
  return { raw, call, viewer, admin, edits }
}

const NOTE = { id: 1, at: '2026-10-13', text: 'Scope change: oa-gcs-usage-dvx left the meta store', author: 'ops@example.test', created: T, updated: T, path: null, url: null, kind: null }

describe('/api/annotations', () => {
  it('a viewer reads an empty list; an admin adds a note (audited); the viewer then reads it', async () => {
    const { call, viewer, admin, edits } = await setup()
    expect(await call('GET', '', { token: viewer })).toEqual([200, { annotations: [], applied: true }])
    expect(await call('POST', '', { token: admin, body: { at: '2026-10-13', text: `  ${NOTE.text}\n` } })).toEqual([201, { annotation: NOTE }])
    expect(await call('GET', '', { token: viewer })).toEqual([200, { annotations: [NOTE], applied: true }])
    expect(edits()).toEqual([{ tbl: 'chart_annotations', pk: '1', action: 'insert', who: 'ops@example.test', ts: T, old_json: null, new_json: NOTE }])
  })

  it('notes list by `at`, then id', async () => {
    const { call, admin, viewer } = await setup()
    for (const at of ['2026-10-13', '2026-09-01T1200', '2026-10-13']) await call('POST', '', { token: admin, body: { at, text: `n ${at}` } })
    const [, body] = await call('GET', '', { token: viewer })
    expect((body as { annotations: { id: number; at: string }[] }).annotations.map(a => [a.id, a.at])).toEqual([[2, '2026-09-01T1200'], [1, '2026-10-13'], [3, '2026-10-13']])
  })

  it('an admin edits (the editor becomes the author) and deletes, each audited', async () => {
    const { raw, call, admin, viewer, edits } = await setup()
    await call('POST', '', { token: admin, body: { at: NOTE.at, text: NOTE.text } })
    raw.prepare("UPDATE chart_annotations SET author = 'first@example.test'").run()
    const first = { ...NOTE, author: 'first@example.test' }
    vi.setSystemTime(NOW + 60_000)
    const edited = { ...NOTE, text: 'Scope change, not a deletion', updated: T + 60 }
    expect(await call('PATCH', '', { token: admin, body: { id: 1, text: edited.text } })).toEqual([200, { annotation: edited }])
    expect(await call('GET', '', { token: viewer })).toEqual([200, { annotations: [edited], applied: true }])
    expect(await call('DELETE', '?id=1', { token: admin })).toEqual([200, { deleted: edited }])
    expect(await call('GET', '', { token: viewer })).toEqual([200, { annotations: [], applied: true }])
    expect(edits().map(e => [e.action, e.ts, e.old_json, e.new_json])).toEqual([
      ['insert', T, null, NOTE],
      ['update', T + 60, first, edited],
      ['delete', T + 60, edited, null],
    ])
  })

  it('a non-admin can’t write: a viewer is 403, anonymous 401; nothing changes', async () => {
    const { call, viewer, admin, edits } = await setup()
    await call('POST', '', { token: admin, body: { at: NOTE.at, text: NOTE.text } })
    expect([
      await call('POST', '', { token: viewer, body: { at: '2026-10-14', text: 'x' } }),
      await call('PATCH', '', { token: viewer, body: { id: 1, text: 'x' } }),
      await call('DELETE', '?id=1', { token: viewer }),
      await call('POST', '', { body: { at: '2026-10-14', text: 'x' } }),
      await call('GET', ''),
    ]).toEqual([
      [403, { error: 'forbidden' }],
      [403, { error: 'forbidden' }],
      [403, { error: 'forbidden' }],
      [401, { error: 'unauthenticated' }],
      [401, { error: 'unauthenticated' }],
    ])
    expect(await call('GET', '', { token: viewer })).toEqual([200, { annotations: [NOTE], applied: true }])
    expect(edits().length).toEqual(1)
  })

  it('a secondary store’s notes are its own, behind its scope; an unknown store is a 404', async () => {
    const { call, viewer, admin } = await setup()
    const meta = { ...NOTE, at: '2026-10-12' }
    expect(await call('POST', '?store=meta', { token: admin, body: { at: meta.at, text: meta.text } })).toEqual([201, { annotation: meta }])
    expect([
      await call('GET', '?store=meta', { token: admin }),
      await call('GET', '', { token: admin }),
      await call('GET', '?store=primary', { token: admin }),
      await call('GET', '?store=meta', { token: viewer }),
      await call('PATCH', '', { token: admin, body: { id: 1, text: 'x' } }),
      await call('GET', '?store=nope', { token: admin }),
    ]).toEqual([
      [200, { annotations: [meta], applied: true }],
      [200, { annotations: [], applied: true }],
      [200, { annotations: [], applied: true }],
      [403, { error: 'forbidden' }],
      [404, { error: 'no such annotation' }],
      [404, { error: 'unknown store \'nope\'' }],
    ])
  })

  it('a D1 without the table (migration not applied): GET is an empty list, a write a 503', async () => {
    const { raw, call, viewer, admin } = await setup()
    raw.exec('DROP TABLE chart_annotations')
    expect([
      await call('GET', '', { token: viewer }),
      await call('POST', '', { token: admin, body: { at: '2026-10-13', text: 'x' } }),
      await call('DELETE', '?id=1', { token: admin }),
    ]).toEqual([
      [200, { annotations: [], applied: false }],
      [503, { error: NOT_APPLIED }],
      [503, { error: NOT_APPLIED }],
    ])
  })

  it('a note scoped to a path, linked and kinded; a patch changes or clears just those', async () => {
    const { call, admin, viewer } = await setup()
    const url = 'https://openathena.slack.com/archives/C0AHF5KV11Q/p1790109588734129'
    const ask = { ...NOTE, at: '2026-09-22', text: 'Asked recent adders to clean up', path: 'marin-us-east-02a', url, kind: 'cleanup ask' }
    expect(await call('POST', '', { token: admin, body: { at: ask.at, text: ask.text, path: '/marin-us-east-02a/', url, kind: ' cleanup ask ' } })).toEqual([201, { annotation: ask }])
    const moved = { ...ask, path: 'marin-us-east-02a/marin', kind: null }
    expect(await call('PATCH', '', { token: admin, body: { id: 1, path: 'marin-us-east-02a/marin', kind: '' } })).toEqual([200, { annotation: moved }])
    expect(await call('GET', '', { token: viewer })).toEqual([200, { annotations: [moved], applied: true }])
  })

  it('a D1 before migration chart-annotation-scope: notes read as every chart’s; a write is a 503 naming it', async () => {
    const { raw, call, viewer, admin } = await setup()
    for (const c of ['path', 'url', 'kind']) raw.exec(`ALTER TABLE chart_annotations DROP COLUMN ${c}`)
    raw.prepare("INSERT INTO chart_annotations (store, at, text, author, created, updated) VALUES ('primary', '2026-10-13', 'old', 'ops@example.test', 1, 1)").run()
    expect([
      await call('GET', '', { token: viewer }),
      await call('POST', '', { token: admin, body: { at: '2026-10-14', text: 'x' } }),
      await call('PATCH', '', { token: admin, body: { id: 1, text: 'y' } }),
    ]).toEqual([
      [200, { annotations: [{ id: 1, at: '2026-10-13', text: 'old', author: 'ops@example.test', created: 1, updated: 1, path: null, url: null, kind: null }], applied: true }],
      [503, { error: SCOPE_NOT_APPLIED }],
      [503, { error: SCOPE_NOT_APPLIED }],
    ])
  })

  it('bad writes are 400s', async () => {
    const { call, admin } = await setup()
    expect([
      await call('POST', '', { token: admin, body: { at: '10/13', text: 'x' } }),
      await call('POST', '', { token: admin, body: { at: '2026-10-13', text: '   ' } }),
      await call('PATCH', '', { token: admin, body: { text: 'x' } }),
      await call('DELETE', '?id=x', { token: admin }),
    ]).toEqual([
      [400, { error: 'at: want YYYY-MM-DD or YYYY-MM-DDTHHMM' }],
      [400, { error: 'text: empty' }],
      [400, { error: 'id: want an integer' }],
      [400, { error: 'id: want an integer' }],
    ])
  })
})

describe('parseNote', () => {
  it('a full note, a partial patch, and each refusal', () => {
    expect([
      parseNote({ at: '2026-10-13T0700', text: ' a ' }),
      parseNote({ text: 'b' }, true),
      parseNote({ at: '2026-10-13' }, true),
      parseNote({}, true),
      parseNote({ at: '2026-02-30', text: 'a' }),
      parseNote({ at: '2026-10-13', text: 'x'.repeat(2001) }),
      parseNote({ at: '2026-10-13', text: 3 }),
      parseNote([]),
    ]).toEqual([
      { at: '2026-10-13T0700', text: 'a' },
      { text: 'b' },
      { at: '2026-10-13' },
      { error: 'nothing to change (at, text, path, url, kind)' },
      { error: 'at: want YYYY-MM-DD or YYYY-MM-DDTHHMM' },
      { error: 'text: over 2000 characters' },
      { error: 'text: want a string' },
      { error: 'want a JSON object' },
    ])
  })

  it('path, url and kind: normalized, cleared by null or empty, and each refusal', () => {
    expect([
      parseNote({ path: 'a//b/', url: ' https://x.test/p ', kind: ' ask ' }, true),
      parseNote({ path: '/', url: '', kind: null }, true),
      parseNote({ path: 'a/../b' }, true),
      parseNote({ path: 3 }, true),
      parseNote({ url: 'javascript:alert(1)' }, true),
      parseNote({ url: 'not a url' }, true),
      parseNote({ kind: 'k'.repeat(33) }, true),
    ]).toEqual([
      { path: 'a/b', url: 'https://x.test/p', kind: 'ask' },
      { path: null, url: null, kind: null },
      { error: 'path: no . or .. segments' },
      { error: 'path: want a string or null' },
      { error: 'url: want http(s)' },
      { error: 'url: not a URL' },
      { error: 'kind: over 32 characters' },
    ])
  })
})
