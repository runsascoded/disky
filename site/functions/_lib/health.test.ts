// specs/health-page.md: /api/health over a fake `INDEX_R2` (both stores' `scans.json`, manifests and run files) and the
// cw lineage's D1 (`index_schema` path rows, `scan_runs`). The interval store never got scan 04; the static index's
// run for 05 has no `drill/`; scan 07's job is running.
import { describe, expect, it } from 'vitest'
import { onRequestGet as route } from '../api/health'
import { IV_RUN_FILES } from './health'
import { sqliteD1 } from './testD1'
import type { HealthDoc } from '../../src/healthModel'

const D = (d: string) => `2026-10-${d}`
const UPLOADED = new Date(Date.UTC(2026, 9, 7, 6))

/** An `R2Bucket` over `objects` (key → JSON body, or '' for a file whose content doesn't matter). */
function r2(objects: Record<string, unknown>): R2Bucket {
  const obj = (key: string) => ({ key, uploaded: UPLOADED, size: 1, json: async () => objects[key] })
  return {
    head: async (key: string) => (key in objects ? obj(key) : null),
    get: async (key: string) => (key in objects ? obj(key) : null),
    list: async ({ prefix }: { prefix: string }) => ({ objects: Object.keys(objects).filter(k => k.startsWith(prefix)).sort().map(obj), truncated: false }),
  } as unknown as R2Bucket
}

const runs = {
  iv: [
    { key: 'deltas/2026-10-05', first: D('05'), last: D('05'), level: 0, scans: [D('05')], rows: 10, bytes: 100 },
    { key: 'deltas/2026-10-06', first: D('06'), last: D('06'), level: 0, scans: [D('06')], rows: 12, bytes: 120 },
  ],
  st: [
    { key: 'deltas/2026-10-03_2026-10-04', first: D('03'), last: D('04'), level: 1, scans: [D('03'), D('04')] },
    { key: 'deltas/2026-10-05', first: D('05'), last: D('05'), level: 0, scans: [D('05')] },
  ],
}
const TIERS = ['catalog/meta.json', 'drill/meta.json', 'anchors/meta.json']
const OBJECTS: Record<string, unknown> = {
  'interval-store/iv1/scans.json': { scans: ['01', '02', '03'].map(d => ({ id: D(d), ts: 0 })) },
  'interval-store/iv1/manifests/2026-10-05.json': { scans: [], runs: runs.iv.slice(0, 1) },
  'interval-store/iv1/manifests/2026-10-06.json': { gen: 'iv1', date: D('06'), scans: [], stamps: {}, runs: runs.iv },
  ...Object.fromEntries(runs.iv.flatMap(r => IV_RUN_FILES.map(f => [`interval-store/iv1/${r.key}/${f}`, '']))),
  'static-names/sn1/scans.json': { scans: [{ id: D('01') }, { id: D('02') }] },
  ...Object.fromEntries(TIERS.map(f => [`static-names/sn1/${f}`, ''])),
  'static-names/sn1/manifests/2026-10-05.json': { scans: [], runs: runs.st.slice(0, 1) },
  'static-names/sn1/manifests/2026-10-05.m001.json': { scans: [], runs: runs.st, rev: 1, revises: 'manifests/2026-10-05.json' },
  ...Object.fromEntries(TIERS.map(f => [`static-names/sn1/deltas/2026-10-03_2026-10-04/${f}`, ''])),
  ...Object.fromEntries(TIERS.filter(f => !f.startsWith('drill')).map(f => [`static-names/sn1/deltas/2026-10-05/${f}`, ''])),
}

const SEED = `
INSERT INTO index_schema (store, date, variant, version, schema_json) VALUES
  ${['01', '02', '03', '04', '05'].map(d => `('primary', '${D(d)}', 'path', 2, '[]')`).join(', ')};
INSERT INTO scan_runs (run_id, scan, kind, parent, status, started_ts, finished_ts, source, updated_ts, store) VALUES
  ('r6', '${D('06')}', 'scan', NULL, 'succeeded', 100, 400, 'job', 0, 'primary'),
  ('r7', '${D('07')}', 'scan', NULL, 'running', 1000, NULL, 'job', 0, 'primary');
`

const get = async (env: object) => {
  const r = await route({ request: new Request('http://localhost/api/health'), env: env as never })
  return { status: r.status, cache: r.headers.get('x-health-cache'), doc: (await r.json()) as HealthDoc }
}

describe('GET /api/health', () => {
  it('reads both stores and D1: stacks, coverage gaps, freshness inputs', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(SEED)
    const { status, cache, doc } = await get({ DB: db, INDEX_R2: r2(OBJECTS), INTERVAL_STORE_GEN: 'iv1', STATIC_GEN: 'sn1' })
    expect([status, cache, doc.scans]).toEqual([200, 'miss', ['01', '02', '03', '04', '05', '06', '07'].map(D)])
    expect(doc.stores.map(s => [s.kind, s.gen, s.manifest, s.rev, s.revises, s.manifest_ts, s.base, s.base_tiers ?? null, s.runs.map(r => [r.key, r.level, r.r2, r.tiers ?? null]), s.carries.map(c => c.output), s.error ?? null])).toEqual([
      ['interval', 'iv1', '2026-10-06.json', 0, null, UPLOADED.getTime() / 1000, ['01', '02', '03'].map(D), null,
        [['deltas/2026-10-05', 0, true, null], ['deltas/2026-10-06', 0, true, null]], ['deltas/2026-10-05_2026-10-06'], null],
      ['static', 'sn1', '2026-10-05.m001.json', 1, 'manifests/2026-10-05.json', UPLOADED.getTime() / 1000, ['01', '02'].map(D), { light: true, drill: true, anchors: true },
        [['deltas/2026-10-03_2026-10-04', 1, true, { light: true, drill: true, anchors: true }], ['deltas/2026-10-05', 0, true, { light: true, drill: false, anchors: true }]], [], null],
    ])
    expect(doc.coverage.filter(c => c.gaps.length).map(c => [c.scan, c.gaps])).toEqual([
      [D('04'), ['interval']],
      [D('05'), ['drill']],
      [D('06'), ['light', 'drill', 'anchors']],
    ])
    expect(doc.coverage.find(c => c.scan === D('07'))!.cells).toEqual({ path: 'pending', interval: 'pending', light: 'pending', drill: 'pending', anchors: 'pending', r2: 'na' })
  })

  it('an unconfigured store is absent; a missing run file is a broken run', async () => {
    const objects = { ...OBJECTS }
    delete objects['interval-store/iv1/deltas/2026-10-06/served/bysize.groups.parquet']
    const { doc } = await get({ INDEX_R2: r2(objects), INTERVAL_STORE_GEN: 'iv1' })
    expect([doc.stores.map(s => [s.kind, s.runs.map(r => r.r2)]), doc.coverage.map(c => [c.scan, c.cells.interval, c.cells.r2])]).toEqual([
      [['interval', [true, false]]],
      [[D('01'), 'present', 'present'], [D('02'), 'present', 'present'], [D('03'), 'present', 'present'], [D('05'), 'present', 'present'], [D('06'), 'missing', 'missing']],
    ])
  })

  it('a generation without `scans.json` reports the error, no stack', async () => {
    const { doc } = await get({ INDEX_R2: r2({}), STATIC_GEN: 'sn1' })
    expect(doc.stores.map(s => [s.kind, s.base, s.runs, s.error])).toEqual([['static', [], [], 'static-names/sn1/scans.json: not on R2']])
  })

  it('no stores and no D1: an empty document', async () => {
    const { doc } = await get({})
    expect([doc.scans, doc.stores, doc.coverage]).toEqual([[], [], []])
  })

  it('a store\'s merge status record: merging, then stuck once its heartbeat stops; an unreadable one is none', async () => {
    const now = Math.floor(Date.now() / 1000)
    const plan = { inputs: ['deltas/2026-10-05', 'deltas/2026-10-06'], output: 'deltas/2026-10-05_2026-10-06', level: 1, first: D('05'), last: D('06'), scans: 2 }
    const record = { holder: 'iv-merge-2026-10-06-060000', owner: 'uid:7', state: 'merging', started_ts: now - 1200, updated_ts: now - 60, heartbeat_s: 300,
      lease_s: 14400, manifest: 'manifests/2026-10-06.json', merges: [plan], done: [] }
    const read = async (merging: unknown) => {
      const { doc } = await get({ INDEX_R2: r2({ ...OBJECTS, ...(merging === undefined ? {} : { 'interval-store/iv1/merging.json': merging }) }), INTERVAL_STORE_GEN: 'iv1', STATIC_GEN: 'sn1' })
      return doc.stores.map(s => [s.kind, s.merging ?? null, s.merge && [s.merge.status, s.merge.why], s.carries.map(c => c.output)])
    }
    expect(await read(record)).toEqual([
      ['interval', record, ['merging', null], [plan.output]],
      ['static', null, null, []],
    ])
    expect((await read({ ...record, updated_ts: now - 2400 }))[0].slice(2)).toEqual([['stuck', 'no heartbeat for 40m'], [plan.output]])
    for (const bad of [{ holder: 'x' }, { ...record, merges: undefined }, { ...record, started_ts: '10:00' }]) {
      expect(await read(bad)).toEqual([['interval', null, null, [plan.output]], ['static', null, null, []]])
    }
    expect(await read(undefined)).toEqual([['interval', null, null, [plan.output]], ['static', null, null, []]])
  })
})
