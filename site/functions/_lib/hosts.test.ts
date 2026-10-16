import { describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { forgetHosts, hostsScript, loadHosts, pageHosts, TTL_MS } from './hosts'
import { secondaryStores, withStore } from './stores'
import { sqliteD1 } from './testD1'

// The capture hosts' registry (specs/hosts-registry.md): D1 `hosts` rows become stores with no deploy.

const SEED = `
  INSERT INTO hosts (id, store, label, home, root, mark, created_at) VALUES
    ('m3', 'primary', 'm3', 'Users/ryan', NULL, NULL, '2026-10-16T00:00:00Z'),
    ('nas', 'nas', 'NAS', NULL, 'mnt/user', 'nas', '2026-10-16T00:00:01Z'),
    ('m1', 'm1', 'M1', 'Users/ryan', NULL, NULL, '2026-10-16T00:00:02Z');
`
const BASE = {
  STORE_BUCKET: 'disk-tree',
  STORE_REGION: 'auto',
  STORE_ENDPOINT: 'https://acct.r2.example.test',
  STORE_ACCESS_KEY_ID: 'key-id',
  STORE_SECRET_ACCESS_KEY: 'key-secret',
  SNAPSHOTS_SUBDIR: 'laptop',
  ROOT_LABEL: 'm3',
}
const NAS_JSON = JSON.stringify({ nas: { vars: { ROOT_LABEL: 'nas', SNAPSHOTS_SUBDIR: 'nas', STORE_SCHEME: 'file:///' } } })

async function seeded(extra: Partial<Env> = {}): Promise<Env> {
  const { db, raw } = await sqliteD1()
  raw.exec(SEED)
  const env = { ...BASE, ...extra, DB: db } as Env
  await loadHosts(env)
  return env
}

describe('loadHosts', () => {
  it('reads every row in enrollment order; a D1 without the table reads as none', async () => {
    const env = await seeded()
    expect((await loadHosts(env)).map(h => [h.id, h.store, h.label, h.home, h.root, h.mark, h.revoked_at])).toEqual([
      ['m3', 'primary', 'm3', 'Users/ryan', null, null, null],
      ['nas', 'nas', 'NAS', null, 'mnt/user', 'nas', null],
      ['m1', 'm1', 'M1', 'Users/ryan', null, null, null],
    ])
    const { db, raw } = await sqliteD1()
    raw.exec('DROP TABLE hosts')
    expect(await loadHosts({ DB: db })).toEqual([])
    expect(await loadHosts({})).toEqual([])
  })
  it('caches per DB for `TTL_MS`; `forgetHosts` drops it', async () => {
    const { db, raw } = await sqliteD1()
    const env = { DB: db } as Env
    expect(await loadHosts(env, 1_000)).toEqual([])
    raw.exec(SEED)
    expect(await loadHosts(env, 1_000 + TTL_MS - 1)).toEqual([])
    expect((await loadHosts(env, 1_000 + TTL_MS)).map(h => h.id)).toEqual(['m3', 'nas', 'm1'])
    raw.exec("DELETE FROM hosts WHERE id = 'm1'")
    forgetHosts(env)
    expect((await loadHosts(env, 1_000 + TTL_MS)).map(h => h.id)).toEqual(['m3', 'nas'])
  })
})

describe('host stores', () => {
  it('each non-primary host is a secondary store on the primary’s bucket and keys; a `STORES_JSON` entry wins', async () => {
    const env = await seeded({ STORES_JSON: NAS_JSON })
    expect(secondaryStores(env)).toEqual({
      nas: { vars: { ROOT_LABEL: 'nas', SNAPSHOTS_SUBDIR: 'nas', STORE_SCHEME: 'file:///' } },
      m1: {
        vars: { ROOT_LABEL: 'M1', SNAPSHOTS_SUBDIR: 'm1', STORE_SCHEME: 'file:///', STORE_BUCKETS: '*', STORE_BUCKET: 'disk-tree', STORE_REGION: 'auto' },
        secrets: { STORE_ENDPOINT: 'STORE_ENDPOINT', STORE_ACCESS_KEY_ID: 'STORE_ACCESS_KEY_ID', STORE_SECRET_ACCESS_KEY: 'STORE_SECRET_ACCESS_KEY' },
      },
    })
  })
  it('`store=<host store>` resolves to its overlay; an unenrolled key is still a 404', async () => {
    const env = await seeded()
    const ctx = (qs: string) => withStore({ request: new Request(`https://disk.example.test/api/store?${qs}`), env })
    const m1 = ctx('store=m1')
    if (m1 instanceof Response) throw new Error(`m1: ${m1.status}`)
    const pick = (e: Env) => Object.fromEntries(['STORE_KEY', 'ROOT_LABEL', 'SNAPSHOTS_SUBDIR', 'STORE_SCHEME', 'STORE_BUCKET', 'STORE_ENDPOINT', 'STORE_ACCESS_KEY_ID', 'STORE_SECRET_ACCESS_KEY']
      .map(k => [k, (e as unknown as Record<string, unknown>)[k]]))
    expect(pick(m1.env)).toEqual({
      STORE_KEY: 'm1', ROOT_LABEL: 'M1', SNAPSHOTS_SUBDIR: 'm1', STORE_SCHEME: 'file:///', STORE_BUCKET: 'disk-tree',
      STORE_ENDPOINT: 'https://acct.r2.example.test', STORE_ACCESS_KEY_ID: 'key-id', STORE_SECRET_ACCESS_KEY: 'key-secret',
    })
    const nope = ctx('store=m2')
    expect(nope instanceof Response ? [nope.status, await nope.text()] : null).toEqual([404, '{"error":"unknown store \'m2\'"}\n'])
  })
})

describe('the page stamp', () => {
  it('a member’s page gets the non-primary hosts; a signed-out page nothing', async () => {
    const env = await seeded()
    expect(await pageHosts({ request: new Request('http://localhost:3253/'), env })).toEqual([
      { id: 'nas', store: 'nas', label: 'NAS', root: 'mnt/user', mark: 'nas' },
      { id: 'm1', store: 'm1', label: 'M1', home: 'Users/ryan' },
    ])
    expect(await pageHosts({ request: new Request('https://disk.example.test/'), env })).toBe(null)
  })
  it('the script escapes `<` so a label can’t close the tag', () => {
    expect(hostsScript([{ id: 'x', store: 'x', label: '</script><b>' }])).toBe(
      '<script>window.__DISKY_HOSTS__=[{"id":"x","store":"x","label":"\\u003c/script>\\u003cb>"}]</script>',
    )
  })
})
