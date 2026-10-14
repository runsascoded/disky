/** Single files assignable and stageable (specs/file-assign.md), as a spec: an
 * exact-object item names its one key — never `key.bak`, never `key/…` — in the
 * ledger's folds, the audit log, the `/api/actions` and `/api/plans/stage`
 * handlers (over a local D1), the dispatch snapshots and the digest; folder
 * items behave as they always did. */
import { describe, expect, it } from 'vitest'
import type { Env } from './auth'
import { actionLog } from './actionLog'
import { computeOwners, foldLatest, newAgg, type OwnerRow, type PathAgg } from './ownerBands'
import { ownerLens } from './owners'
import { canonicalObject, digestLines, planDetail, planDigest, planStagingItems, prefixShape, snapshotPlan, snapshotPlanBuckets, type PlanItem } from './plans'
import { GCS_LEDGER, LEDGER_KIND, migrations, sqliteD1 } from './testD1'
import { ownerIndex } from '../../src/ownerIndex'
import { onRequest as actionsRoute } from '../api/actions'
import { onRequest as plansRoute } from '../api/plans/[[path]]'
import { stageBody, stageMany } from '../../src/batches'

const SHOW = 'gs://b/show/'
const CLIP = 'gs://b/show/clip.mp3'
const pre = (key: string): PlanItem => ({ key, kind: 'prefix' })
const obj = (key: string): PlanItem => ({ key, kind: 'object' })

// ── The ledger's folds ────────────────────────────────────────────────────────

// Bucket `b` (1000 B, attributed to nobody): `show/` holds `clip.mp3` (10),
// its `.bak` sibling (20), `clip.mp3/x` — a key under a folder that shares the
// clip's name (5) — and 70 B of other files.
const agg = (b: number): PathAgg => ({ ...newAgg(), b, o: 1 })
const AGGS = new Map<string, PathAgg>([
  ['b', agg(1000)], ['b/show', agg(105)], ['b/show/clip.mp3', agg(10)], ['b/show/clip.mp3.bak', agg(20)], ['b/show/clip.mp3/x', agg(5)],
])
type Row = { prefix: string; kind?: 'object'; owner: string; ts: number }
const rows = (...rs: Row[]): OwnerRow[] => rs.map((r, i) => ({ ...r, action_id: i + 1, who: 'x' }))
const folder = (owner: string, ts: number, prefix = SHOW): Row => ({ prefix, owner, ts })
const object = (owner: string, ts: number, prefix = CLIP): Row => ({ prefix, kind: 'object', owner, ts })

/** What every fold says for one ledger: the client resolver per path, the
 * server's per-user totals, and each user's lens value of `show/`. */
function folds(ledger: OwnerRow[]) {
  const idx = ownerIndex({ owners: ledger.map(r => ({ ...r, memo: null, who: r.who ?? '' })) })
  const who = (uri: string, kind: 'prefix' | 'object') => idx.assignmentOf(uri, kind)?.who ?? null
  const t = computeOwners({ owners: foldLatest(ledger), aggs: AGGS, buckets: ['b'] })
  const lens = (u: string) => ownerLens(t.assignments, u)!.value('b/show', 105, 0)
  return {
    resolve: {
      clip: who(CLIP, 'object'),
      bak: who('gs://b/show/clip.mp3.bak', 'object'),
      underSameName: who('gs://b/show/clip.mp3/x', 'object'),
      sameNameFolder: who('gs://b/show/clip.mp3/', 'prefix'),
      show: who(SHOW, 'prefix'),
    },
    users: Object.fromEntries(Object.entries(t.users).map(([u, v]) => [u, v.b])),
    assignments: t.assignments.map(a => [a.prefix, a.kind ?? 'prefix', a.owner, a.bytes, a.repainted_by ?? null]),
    lens: { ann: lens('ann'), bob: lens('bob') },
  }
}

describe('ledger resolution: file vs folder, nested, newest wins', () => {
  it('an object newer than its folder owns exactly its key (not `.bak`, not `key/…`)', () => {
    expect(folds(rows(folder('ann', 1), object('bob', 2)))).toEqual({
      resolve: { clip: 'bob', bak: 'ann', underSameName: 'ann', sameNameFolder: 'ann', show: 'ann' },
      users: { ann: 95, bob: 10 },
      assignments: [[SHOW, 'prefix', 'ann', 105, null], [CLIP, 'object', 'bob', 10, null]],
      lens: { ann: 95, bob: 10 },
    })
  })
  it('a folder newer than the object repaints it (recency beats specificity)', () => {
    expect(folds(rows(object('bob', 1), folder('ann', 2)))).toEqual({
      resolve: { clip: 'ann', bak: 'ann', underSameName: 'ann', sameNameFolder: 'ann', show: 'ann' },
      users: { ann: 105 },
      assignments: [[SHOW, 'prefix', 'ann', 105, null], [CLIP, 'object', 'bob', 10, SHOW]],
      lens: { ann: 105, bob: 0 },
    })
  })
  it('an object alone covers nothing but itself', () => {
    expect(folds(rows(object('bob', 1)))).toEqual({
      resolve: { clip: 'bob', bak: null, underSameName: null, sameNameFolder: null, show: null },
      users: { bob: 10 },
      assignments: [[CLIP, 'object', 'bob', 10, null]],
      lens: { ann: 0, bob: 10 },
    })
  })
  it('nested: a newest whole-bucket folder overrides the object and the folder under it', () => {
    expect(folds(rows(folder('ann', 1), object('bob', 2), folder('carol', 3, 'gs://b/')))).toEqual({
      resolve: { clip: 'carol', bak: 'carol', underSameName: 'carol', sameNameFolder: 'carol', show: 'carol' },
      users: { carol: 1000 },
      assignments: [['gs://b/', 'prefix', 'carol', 1000, null], [SHOW, 'prefix', 'ann', 105, 'gs://b/'], [CLIP, 'object', 'bob', 10, 'gs://b/']],
      lens: { ann: 0, bob: 0 },
    })
  })
  it('a folder sharing the key\'s name (`key/`) owns what is under it, never the object itself', () => {
    expect(folds(rows(object('bob', 1), folder('kim', 2, `${CLIP}/`))).resolve).toEqual({
      clip: 'bob', bak: null, underSameName: 'kim', sameNameFolder: 'kim', show: null,
    })
  })
  it('an object re-assigned: the newest object row wins', () => {
    expect(folds(rows(object('bob', 1), object('ann', 2))).resolve.clip).toBe('ann')
  })
})

// ── The audit log ─────────────────────────────────────────────────────────────

describe('actionLog with object rows', () => {
  it('an object is overridden only by a newer folder above it, superseded only by a newer row on the same object', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(GCS_LEDGER)
    const act = (id: number, prefix: string, kind: 'object' | null, owner: string) => raw.exec(
      `INSERT INTO actions (id, actor, ts, scan, pattern, kind, set_owner, owner) VALUES (${id}, 'u', ${100 + id}, 's', '${prefix}', ${kind ? `'${kind}'` : 'NULL'}, 1, '${owner}');` +
      `INSERT INTO owner_prefixes (action_id, prefix, kind, owner, ts) VALUES (${id}, '${prefix}', ${kind ? `'${kind}'` : 'NULL'}, '${owner}', ${100 + id});`,
    )
    act(1, 'gs://b/a/k.mp3', 'object', 'bob')   // under a newer folder: overridden
    act(2, 'gs://b/a/', null, 'ann')
    act(3, 'gs://b/a/k.mp3', 'object', 'bob')   // newer than the folder: live
    act(4, 'gs://b/a/k.mp3.bak', 'object', 'kim') // `k.mp3` is a string prefix of it: still live
    act(5, 'gs://b/c/k', 'object', 'kim')
    act(6, 'gs://b/c/k', 'object', 'lee')       // supersedes 5
    const { rows: log } = await actionLog({ DB: db } as Env, 10, 0)
    expect(log.map(r => [r.id, r.prefix, r.kind, r.status])).toEqual([
      [6, 'gs://b/c/k', 'object', 'live'],
      [5, 'gs://b/c/k', 'object', 'superseded'],
      [4, 'gs://b/a/k.mp3.bak', 'object', 'live'],
      [3, 'gs://b/a/k.mp3', 'object', 'live'],
      [2, 'gs://b/a/', 'prefix', 'live'],
      [1, 'gs://b/a/k.mp3', 'object', 'superseded'],
    ])
  })
  it('a newer object never overrides an older object it is a string prefix of', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(GCS_LEDGER)
    raw.exec(`INSERT INTO actions (id, actor, ts, scan, pattern, kind, set_owner, owner) VALUES (1, 'u', 1, 's', 'gs://b/k.mp3.bak', 'object', 1, 'kim'), (2, 'u', 2, 's', 'gs://b/k.mp3', 'object', 1, 'bob');
      INSERT INTO owner_prefixes (action_id, prefix, kind, owner, ts) VALUES (1, 'gs://b/k.mp3.bak', 'object', 'kim', 1), (2, 'gs://b/k.mp3', 'object', 'bob', 2);`)
    expect((await actionLog({ DB: db } as Env, 10, 0)).rows.map(r => [r.id, r.status])).toEqual([[2, 'live'], [1, 'live']])
  })
})

// ── /api/actions over a local D1 ──────────────────────────────────────────────

const ENV = { STAGING: '1', STORE_SCHEME: 'gs://', STORE_BUCKETS: 'b', DEV_EMAIL: 'ann@example.test' }
const answer = async (r: Response) => [r.status, await r.json()]

describe('POST /api/actions with an exact object', () => {
  const post = (env: object, body: unknown) => actionsRoute({ request: new Request('http://localhost/api/actions', { method: 'POST', body: JSON.stringify(body) }), env } as never).then(answer)

  it('records the kind on the action and its owner row, and GET returns it', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(GCS_LEDGER)
    const env = { ...ENV, DB: db }
    const [status] = await post(env, [
      { pattern: SHOW, owner: 'ann', scan: '2026-10-09' },
      { pattern: CLIP, kind: 'object', owner: 'bob', scan: '2026-10-09' },
      { pattern: 'gs://b/top/', kind: 'prefix', owner: 'kim', scan: '2026-10-09' },
    ])
    expect([
      status,
      raw.prepare('SELECT id, pattern, kind, owner FROM actions ORDER BY id').all(),
      raw.prepare('SELECT action_id, prefix, kind, owner FROM owner_prefixes ORDER BY action_id').all(),
    ]).toEqual([200, [
      { id: 1, pattern: SHOW, kind: null, owner: 'ann' },
      { id: 2, pattern: CLIP, kind: 'object', owner: 'bob' },
      { id: 3, pattern: 'gs://b/top/', kind: null, owner: 'kim' },
    ], [
      { action_id: 1, prefix: SHOW, kind: null, owner: 'ann' },
      { action_id: 2, prefix: CLIP, kind: 'object', owner: 'bob' },
      { action_id: 3, prefix: 'gs://b/top/', kind: null, owner: 'kim' },
    ]])
    const [, got] = await actionsRoute({ request: new Request('http://localhost/api/actions'), env } as never).then(answer)
    expect((got as { owners: { prefix: string; kind: string; owner: string }[] }).owners.map(o => [o.prefix, o.kind, o.owner])).toEqual([
      [SHOW, 'prefix', 'ann'], [CLIP, 'object', 'bob'], ['gs://b/top/', 'prefix', 'kim'],
    ])
  })

  it('refuses a malformed object, a slash-less folder, and an unknown kind (nothing written)', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(GCS_LEDGER)
    const env = { ...ENV, DB: db }
    const objErr = 'an object pattern must be gs://<bucket>/<key> over a scanned bucket (the exact key, no trailing slash)'
    expect([
      await post(env, { pattern: 'gs://b/show/', kind: 'object', owner: 'bob' }),
      await post(env, { pattern: 'gs://b/', kind: 'object', owner: 'bob' }),
      await post(env, { pattern: 'gs://other/k.mp3', kind: 'object', owner: 'bob' }),
      await post(env, { pattern: 'gs://b/a/../k', kind: 'object', owner: 'bob' }),
      await post(env, { pattern: CLIP, owner: 'bob' }),
      await post(env, { pattern: CLIP, kind: 'regex', owner: 'bob' }),
      raw.prepare('SELECT COUNT(*) AS n FROM actions').all(),
    ]).toEqual([
      [400, { error: objErr }],
      [400, { error: objErr }],
      [400, { error: objErr }],
      [400, { error: objErr }],
      [400, { error: 'pattern must be gs://<bucket>/<path>/ over a scanned bucket (trailing slash; regex patterns not accepted yet)' }],
      [400, { error: "kind must be 'prefix' or 'object'" }],
      [{ n: 0 }],
    ])
  })
})

// ── Staging, the plan item round trip, the snapshots, the digest ───────────────

describe('planStagingItems: only prefixes cover or absorb', () => {
  it('an object under a staged prefix is covered; a new prefix absorbs staged objects; `.bak` is independent', () => {
    expect(planStagingItems([pre(SHOW)], [obj(CLIP), obj('gs://b/x/k.mp3')])).toEqual({ staged: [obj('gs://b/x/k.mp3')], covered: [obj(CLIP)], absorbed: [] })
    expect(planStagingItems([obj(CLIP), obj('gs://b/x/k.mp3')], [pre(SHOW)])).toEqual({ staged: [pre(SHOW)], covered: [], absorbed: [obj(CLIP)] })
    expect(planStagingItems([obj(CLIP)], [obj(`${CLIP}.bak`), obj(CLIP), pre(`${CLIP}/`)])).toEqual({ staged: [obj(`${CLIP}.bak`), obj(CLIP), pre(`${CLIP}/`)], covered: [], absorbed: [] })
  })
})

describe('canonicalObject', () => {
  const shape = prefixShape({ STORE_SCHEME: 'gs://', STORE_BUCKETS: 'b,c' })!
  it('keeps the key exact; refuses folders, bucket roots, dot segments, backslashes', () => {
    expect(['b/show/clip.mp3', 'gs://c/k', '/b/a b/k(1).mp3', 'gs://b/show/', 'gs://b/', 'b/a/../k', 'b/./k', 'b/a\\k', 'b/a//k'].map(r => canonicalObject(r, shape)))
      .toEqual(['gs://b/show/clip.mp3', 'gs://c/k', 'gs://b/a b/k(1).mp3', null, null, null, null, null, null])
  })
})

describe('POST /api/plans/stage with objects, through to the dispatch snapshots', () => {
  async function staged() {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-10-09', 'path', 2, '[]')`)
    const env = { ...ENV, DB: db }
    const stage = (body: unknown) => plansRoute({ request: new Request('http://localhost/api/plans/stage', { method: 'POST', body: JSON.stringify(body) }), env } as never).then(answer)
    return { db, raw, env, stage }
  }

  it('stages exact objects beside prefixes; the plan item round-trips with its kind', async () => {
    const { db, raw, stage } = await staged()
    expect([
      await stage({ prefixes: ['gs://b/tmp/'], objects: [CLIP, `${CLIP}.bak`, 'gs://b/tmp/x.bin'] }),
      await stage({ objects: [CLIP] }),
      await stage({ objects: ['gs://b/show/'] }),
    ]).toEqual([
      [201, { plan_id: 1, batch_id: 1, staged: ['gs://b/tmp/', CLIP, `${CLIP}.bak`], staged_objects: [CLIP, `${CLIP}.bak`], covered: [], absorbed: [], as_of: '2026-10-09' }],
      [201, { plan_id: 1, batch_id: 2, staged: [CLIP], staged_objects: [CLIP], covered: [], absorbed: [], as_of: '2026-10-09' }],
      [400, { error: 'bad object key "gs://b/show/"' }],
    ])
    expect(raw.prepare('SELECT prefix, kind, batch_id FROM plan_items ORDER BY prefix').all()).toEqual([
      { prefix: CLIP, kind: 'object', batch_id: 1 },
      { prefix: `${CLIP}.bak`, kind: 'object', batch_id: 1 },
      { prefix: 'gs://b/tmp/', kind: null, batch_id: 1 },
    ])
    expect((await planDetail(db, 1, true))!.items.map(i => [i.prefix, i.kind])).toEqual([
      [CLIP, 'object'], [`${CLIP}.bak`, 'object'], ['gs://b/tmp/', 'prefix'],
    ])
    const shape = prefixShape(ENV)!
    expect([await snapshotPlanBuckets(db, 1, shape), await snapshotPlan(db, 1, shape)]).toEqual([
      { plan_id: 1, name: 'Staged', sweep: ['gs://b/tmp/'], objects: [CLIP, `${CLIP}.bak`], buckets: ['b'], as_of: { [CLIP]: '2026-10-09', [`${CLIP}.bak`]: '2026-10-09', 'gs://b/tmp/': '2026-10-09' } },
      { plan_id: 1, name: 'Staged', bucket: 'b', sweep: ['tmp/'], objects: ['show/clip.mp3', 'show/clip.mp3.bak'], as_of: { 'show/clip.mp3': '2026-10-09', 'show/clip.mp3.bak': '2026-10-09', 'tmp/': '2026-10-09' } },
    ])
  })

  it('staging the folder absorbs its staged objects; an object under a staged folder is covered', async () => {
    const { raw, stage } = await staged()
    await stage({ objects: [CLIP, 'gs://b/other/k'] })
    expect([
      await stage({ prefixes: [SHOW] }),
      await stage({ objects: [`${CLIP}.bak`] }),
      raw.prepare('SELECT prefix, kind FROM plan_items ORDER BY prefix').all(),
    ]).toEqual([
      [201, { plan_id: 1, batch_id: 2, staged: [SHOW], covered: [], absorbed: [CLIP], as_of: '2026-10-09' }],
      [201, { plan_id: 1, batch_id: 3, staged: [], covered: [`${CLIP}.bak`], absorbed: [], as_of: '2026-10-09' }],
      [{ prefix: 'gs://b/other/k', kind: 'object' }, { prefix: SHOW, kind: null }],
    ])
  })

  it('a prefix-only plan snapshots and digests exactly as before; an item\'s kind changes the digest', async () => {
    const { db, stage } = await staged()
    await stage({ prefixes: ['gs://b/tmp/', SHOW] })
    const shape = prefixShape(ENV)!
    expect([await snapshotPlanBuckets(db, 1, shape), await snapshotPlan(db, 1, shape)]).toEqual([
      { plan_id: 1, name: 'Staged', sweep: [SHOW, 'gs://b/tmp/'], buckets: ['b'], as_of: { [SHOW]: '2026-10-09', 'gs://b/tmp/': '2026-10-09' } },
      { plan_id: 1, name: 'Staged', bucket: 'b', sweep: ['show/', 'tmp/'], as_of: { 'show/': '2026-10-09', 'tmp/': '2026-10-09' } },
    ])
    const prefixOnly = await planDigest(digestLines([pre(SHOW), pre('gs://b/tmp/')]))
    expect([
      prefixOnly === await planDigest([SHOW, 'gs://b/tmp/']),
      digestLines([pre(SHOW), obj(CLIP)]),
      await planDigest(digestLines([obj('gs://b/k')])) === await planDigest(digestLines([pre('gs://b/k')])),
    ]).toEqual([true, [SHOW, `=${CLIP}`], false])
  })

  it('the bulk bar\'s submission: mixed items in one gesture, posted as `prefixes` + `objects`', async () => {
    const { raw, env } = await staged()
    const bodies: unknown[] = []
    const post = (async (url: string, method: string, body: unknown) => {
      bodies.push(body)
      const r = await plansRoute({ request: new Request(`http://localhost${url}`, { method, body: JSON.stringify(body) }), env } as never)
      const j = await r.json()
      if (!r.ok) throw new Error(JSON.stringify(j))
      return j
    }) as never
    const res = await stageMany([pre('gs://b/tmp/'), obj(CLIP)], ' why ', '2026-10-09', post)
    expect([
      bodies,
      res,
      raw.prepare('SELECT prefix, kind FROM plan_items ORDER BY prefix').all(),
      stageBody([pre('gs://b/tmp/')], undefined, null),
    ]).toEqual([
      [{ prefixes: ['gs://b/tmp/'], objects: [CLIP], note: 'why', as_of: '2026-10-09' }],
      { plan_id: 1, batch_id: 1, staged: ['gs://b/tmp/', CLIP], staged_objects: [CLIP], covered: [], absorbed: [], as_of: '2026-10-09' },
      [{ prefix: CLIP, kind: 'object' }, { prefix: 'gs://b/tmp/', kind: null }],
      { prefixes: ['gs://b/tmp/'], note: undefined, as_of: null },
    ])
  })
})

// ── The migrations: additive, foreign keys intact ─────────────────────────────

describe('the `kind` migrations', () => {
  it('cw 0017 adds a nullable, checked `plan_items.kind` over seeded rows, foreign keys intact', async () => {
    const { raw } = await sqliteD1('cw', { before: '0017' })
    raw.exec(`INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'ann', 1);
      INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, 'gs://b/tmp/', 'ann', 1);`)
    raw.exec((await migrations('cw')).find(m => m.name === '0017_plan_items_kind.sql')!.sql)
    raw.exec(`INSERT INTO plan_items (plan_id, prefix, added_by, added_ts, kind) VALUES (1, '${CLIP}', 'ann', 2, 'object')`)
    let bad: string | null = null
    try { raw.exec(`INSERT INTO plan_items (plan_id, prefix, added_by, added_ts, kind) VALUES (1, 'gs://b/z', 'ann', 3, 'prefix')`) } catch (e) { bad = (e as Error).message }
    expect([
      raw.prepare('SELECT prefix, kind FROM plan_items ORDER BY prefix').all(),
      raw.prepare('PRAGMA foreign_key_check').all(),
      bad,
    ]).toEqual([
      [{ prefix: CLIP, kind: 'object' }, { prefix: 'gs://b/tmp/', kind: null }],
      [],
      'CHECK constraint failed: kind IS NULL OR kind = \'object\'',
    ])
  })

  it('the gcs ledger half adds `kind` to `actions` / `owner_prefixes` in place (no rebuild of the referenced table)', async () => {
    const { raw } = await sqliteD1('cw')
    raw.exec(GCS_LEDGER.replace(LEDGER_KIND, ''))
    raw.exec(`INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner) VALUES (1, 'ann', 1, 's', '${SHOW}', 1, 'ann');
      INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (1, '${SHOW}', 'ann', 1);`)
    raw.exec(LEDGER_KIND)
    expect([
      raw.prepare('SELECT a.id, a.kind AS ak, o.kind AS ok FROM actions a JOIN owner_prefixes o ON o.action_id = a.id').all(),
      raw.prepare('PRAGMA foreign_key_check').all(),
    ]).toEqual([[{ id: 1, ak: null, ok: null }], []])
  })
})
