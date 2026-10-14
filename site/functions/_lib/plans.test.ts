import { describe, expect, it } from 'vitest'
import { auditRunControl, bucketOf, canonicalPrefix, covers, emptiedBatches, mergeBatches, planBucket, planDetail, planDigest, PlanSpansBuckets, planStaging, prefixShape, realGate, relPrefix, type RunRow, stageItems, uncovered } from './plans'
import { LINEAGES, type Sqlite, sqliteD1 } from './testD1'

// An S3 deployment scanning two buckets, the first its primary.
const P = 'primary-bucket'
const H = 'second-bucket'
const S3 = prefixShape({ STORE_SCHEME: 's3://', STORE_BUCKETS: `${P},${H}` })!

describe('bucketOf', () => {
  it('reads the bucket off `s3://<b>/…`, `<b>/…` or a bare `<b>`', () => {
    expect(bucketOf(`s3://${H}/tmp/`, S3.buckets)).toBe(H)
    expect(bucketOf(`${H}/tmp/ttl=14d/`, S3.buckets)).toBe(H)
    expect(bucketOf(`/${H}/tmp/`, S3.buckets)).toBe(H)
    expect(bucketOf(H, S3.buckets)).toBe(H)
    expect(bucketOf(`s3://${P}/team/`, S3.buckets)).toBe(P)
  })
  it('anything else is the primary — including a dir that merely starts with a bucket’s name', () => {
    expect(bucketOf('team/checkpoints/', S3.buckets)).toBe(P)
    expect(bucketOf(`${H}-old/x/`, S3.buckets)).toBe(P)
    expect(bucketOf('s3://other-bucket/x/', S3.buckets)).toBe(P)
  })
})

describe('canonicalPrefix', () => {
  it('stores `s3://<bucket>/<rel>/` with the bucket the raw names', () => {
    expect(canonicalPrefix('team/checkpoints', S3)).toBe(`s3://${P}/team/checkpoints/`)
    expect(canonicalPrefix(`s3://${P}/team/checkpoints/`, S3)).toBe(`s3://${P}/team/checkpoints/`)
    expect(canonicalPrefix(`${H}/tmp/ttl=14d`, S3)).toBe(`s3://${H}/tmp/ttl=14d/`)
    expect(canonicalPrefix(`s3://${H}/team/`, S3)).toBe(`s3://${H}/team/`)
  })
  it('rejects the bucket root, `.`/`..` segments and backslashes', () => {
    expect(canonicalPrefix(`s3://${H}/`, S3)).toBeNull()
    expect(canonicalPrefix('../x/', S3)).toBeNull()
    expect(canonicalPrefix('a\\b/', S3)).toBeNull()
  })
  it('relPrefix strips only the given bucket', () => {
    expect(relPrefix(`s3://${H}/tmp/`, H)).toBe('tmp/')
    expect(relPrefix(`s3://${H}/tmp/`, P)).toBe(`${H}/tmp/`)
  })
})

describe('planBucket', () => {
  it('one bucket: that bucket and the items relative to it', () => {
    expect(planBucket([`s3://${H}/tmp/ttl=14d/`, `s3://${H}/team/old/`], S3.buckets)).toEqual({ bucket: H, sweep: ['tmp/ttl=14d/', 'team/old/'] })
    expect(planBucket([`s3://${P}/tmp/x/`], S3.buckets)).toEqual({ bucket: P, sweep: ['tmp/x/'] })
  })
  it('no items: the primary', () => {
    expect(planBucket([], S3.buckets)).toEqual({ bucket: P, sweep: [] })
  })
  it('two buckets: refused, naming both', () => {
    let err: unknown
    try { planBucket([`s3://${P}/tmp/x/`, `s3://${H}/tmp/y/`], S3.buckets) } catch (e) { err = e }
    expect(err).toBeInstanceOf(PlanSpansBuckets)
    expect((err as PlanSpansBuckets).buckets).toEqual([P, H])
    expect((err as Error).message).toBe(`plan spans buckets: ${P}, ${H}`)
  })
})

describe('prefixShape — the deployment\'s scheme + bucket set from [vars]', () => {
  const GCS = prefixShape({ STORE_SCHEME: 'gs://', STORE_BUCKETS: 'gcs-a, gcs-b,gcs-c' })!
  it('no buckets = no shape (the plans and sweep routes refuse); the scheme defaults to `s3://`', () => {
    expect([prefixShape({}), prefixShape({ STORE_BUCKETS: ' , ' }), prefixShape({ STORE_SCHEME: 'gs://' })]).toEqual([null, null, null])
    expect(prefixShape({ STORE_BUCKETS: 'x' })).toEqual({ scheme: 's3://', buckets: ['x'] })
  })
  it('gcs: `gs://<bucket>/<path>/`, the bucket read off the raw over the store\'s set', () => {
    expect(GCS).toEqual({ scheme: 'gs://', buckets: ['gcs-a', 'gcs-b', 'gcs-c'] })
    expect(canonicalPrefix('gs://gcs-b/checkpoints/run/', GCS)).toBe('gs://gcs-b/checkpoints/run/')
    expect(canonicalPrefix('gs://gcs-b/checkpoints/run', GCS)).toBe('gs://gcs-b/checkpoints/run/')
    expect(canonicalPrefix('gcs-c/x/', GCS)).toBe('gs://gcs-c/x/')
    expect(bucketOf('gs://gcs-c/x/', GCS.buckets)).toBe('gcs-c')
  })
  it('an unknown bucket canonicalizes under the primary, as on S3', () => {
    expect(canonicalPrefix('gs://other/x/', GCS)).toBe('gs://gcs-a/other/x/')
  })
})

describe('covers / uncovered — the no-nesting rule', () => {
  it('a prefix covers itself and its descendants, not a sibling that shares its name as a prefix', () => {
    expect(covers('s3://b/a/', 's3://b/a/')).toBe(true)
    expect(covers('s3://b/a/', 's3://b/a/x/y/')).toBe(true)
    expect(covers('s3://b/a/', 's3://b/ab/')).toBe(false)
    expect(covers('s3://b/a/x/', 's3://b/a/')).toBe(false)
  })
  it('uncovered keeps only the outermost prefixes', () => {
    expect(uncovered(['s3://b/a/', 's3://b/a/x/', 's3://b/c/', 's3://b/a/y/z/'])).toEqual(['s3://b/a/', 's3://b/c/'])
    expect(uncovered([])).toEqual([])
  })
})

describe('planStaging — one gesture against the plan', () => {
  const have = ['s3://b/a/', 's3://b/c/d/']
  it('a new prefix under a staged one is covered; one over staged ones absorbs them', () => {
    expect(planStaging(have, ['s3://b/a/x/', 's3://b/c/', 's3://b/e/'])).toEqual({
      staged: ['s3://b/c/', 's3://b/e/'],
      covered: ['s3://b/a/x/'],
      absorbed: ['s3://b/c/d/'],
    })
  })
  it('re-staging an existing prefix is a no-op stage (kept, nothing absorbed)', () => {
    expect(planStaging(have, ['s3://b/a/'])).toEqual({ staged: ['s3://b/a/'], covered: [], absorbed: [] })
  })
  it('an empty plan stages everything', () => {
    expect(planStaging([], ['s3://b/a/'])).toEqual({ staged: ['s3://b/a/'], covered: [], absorbed: [] })
  })
  it('matches the quadratic definitions (pairwise `covers`) on random nested prefix sets', () => {
    const naiveUncovered = (all: readonly string[]) => all.filter(p => !all.some(o => o !== p && covers(o, p)))
    const naiveStaging = (have: readonly string[], add: readonly string[]) => {
      const staged: string[] = []
      const covered: string[] = []
      for (const p of add) {
        if (have.includes(p)) { staged.push(p); continue }
        if (have.some(h => covers(h, p))) covered.push(p)
        else staged.push(p)
      }
      return { staged, covered, absorbed: have.filter(h => !staged.includes(h) && staged.some(p => covers(p, h))) }
    }
    let seed = 7
    const rand = (n: number) => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed % n }
    const prefix = () => `s3://b/${Array.from({ length: 1 + rand(3) }, () => 'abc'[rand(3)] + (rand(2) ? 'x' : '')).join('/')}${rand(5) ? '/' : ''}`
    for (let t = 0; t < 300; t++) {
      const have = naiveUncovered(Array.from({ length: rand(12) }, prefix))
      const add = Array.from({ length: rand(12) }, prefix)
      expect([uncovered(add), planStaging(have, uncovered(add))]).toEqual([naiveUncovered(add), naiveStaging(have, naiveUncovered(add))])
    }
  })
})

describe('planDigest — sha-256 of the sorted prefixes joined by `\\n`, first 16 hex', () => {
  it('pins the value, and is order-independent', async () => {
    expect(await Promise.all([
      planDigest(['s3://b/x/', 's3://b/y/']),
      planDigest(['s3://b/y/', 's3://b/x/']),
      planDigest(['s3://b/x/']),
      planDigest([]),
    ])).toEqual(['ec4acfe3116b50a3', 'ec4acfe3116b50a3', '979e77d52f39c350', 'e3b0c44298fc1c14'])
  })
})

describe('realGate', () => {
  const run = (o: Partial<RunRow>): RunRow => ({
    run_id: 'cw-sweep-dry-1', mode: 'dry', scan: '2026-09-28T1201', actor: 'ann@openathena.ai', started_ts: 100,
    finished_ts: 200, deleted_bytes: 2 * 1024 ** 4, deleted_objects: 1234, skipped_gone: 0, skipped_overwritten: 0,
    plan_digest: 'D1', undo_deadline: null, ...o,
  })
  it('needs a finished dry-run of exactly the current set, and nothing in flight', () => {
    const dry = run({})
    expect(realGate([], 'D1', 0)).toEqual({ ok: false, reason: 'the plan is empty' })
    expect(realGate([], 'D1', 3)).toEqual({ ok: false, reason: 'no dry-run of this plan yet' })
    expect(realGate([dry], 'D2', 3)).toEqual({ ok: false, reason: 'the plan changed since the last dry-run; dry-run it again' })
    expect(realGate([run({ finished_ts: null })], 'D1', 3)).toEqual({ ok: false, reason: 'a dry run is in progress (cw-sweep-dry-1)' })
    expect(realGate([dry], 'D1', 3)).toEqual({ ok: true, dry })
    // the newest matching dry-run wins
    const newer = run({ run_id: 'cw-sweep-dry-2', started_ts: 300, finished_ts: 400, deleted_bytes: 5 })
    expect(realGate([dry, newer], 'D1', 3)).toEqual({ ok: true, dry: newer })
  })
  it('a dry-run without a digest (NULL, or the empty one: ended without a result) never opens it', () => {
    expect(realGate([run({ plan_digest: null })], 'D1', 3)).toEqual({ ok: false, reason: 'the plan changed since the last dry-run; dry-run it again' })
    expect(realGate([run({ plan_digest: '' })], '', 3)).toEqual({ ok: false, reason: 'the plan changed since the last dry-run; dry-run it again' })
  })
})

describe('a `*` bucket list (a filesystem-root store)', () => {
  const shape = prefixShape({ STORE_SCHEME: 'file:///', STORE_BUCKETS: '*' })!
  it('takes each prefix’s first segment as its bucket — no fallback to a primary', () => {
    expect(bucketOf('file:///Applications/Slack.app/', shape.buckets)).toBe('Applications')
    expect(bucketOf('Users/ryan/c/', shape.buckets)).toBe('Users')
  })
  it('canonicalizes outside-home prefixes where they are, not under `Users`', () => {
    expect(canonicalPrefix('file:///Applications/Slack.app', shape)).toBe('file:///Applications/Slack.app/')
    expect(canonicalPrefix('file:///Users/ryan/c/x/', shape)).toBe('file:///Users/ryan/c/x/')
  })
})

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  describe('auditRunControl — who stopped / undid / purged a run, in `admin_edits`', () => {
    it('appends one row per control', async () => {
      const { db, raw } = await sqliteD1(lineage)
      await auditRunControl(db, 'stop', 'gcs-sweep-real-20261005-024600z', 'admin@example.org', null)
      await auditRunControl(db, 'undo', 'run-1', 'admin@example.org', 'gcs-undo-20261007-120000z')
      const rows = raw.prepare("SELECT tbl, pk, action, who, old_json, new_json FROM admin_edits ORDER BY id").all()
      expect(rows).toEqual([
        { tbl: 'deletion_runs', pk: 'gcs-sweep-real-20261005-024600z', action: 'update', who: 'admin@example.org', old_json: null, new_json: '{"control":"stop","job_id":null}' },
        { tbl: 'deletion_runs', pk: 'run-1', action: 'update', who: 'admin@example.org', old_json: null, new_json: '{"control":"undo","job_id":"gcs-undo-20261007-120000z"}' },
      ])
    })
  })

  describe('stageItems — one gesture is all-or-nothing', () => {
    const GS = prefixShape({ STORE_SCHEME: 'gs://', STORE_BUCKETS: 'b' })!
    // A statement failing mid-gesture (here: the item insert, on one prefix)
    // must leave no plan, batch, item, absorbed-delete or audit row behind.
    const boom = "CREATE TRIGGER boom BEFORE INSERT ON plan_items WHEN NEW.prefix = 'gs://b/boom/' BEGIN SELECT RAISE(ABORT, 'boom'); END"
    const dump = (raw: Sqlite) => ({
      plans: raw.prepare('SELECT id, name, state FROM plans ORDER BY id').all(),
      batches: raw.prepare('SELECT id, plan_id, note FROM stage_batches ORDER BY id').all(),
      items: raw.prepare('SELECT plan_id, prefix, batch_id FROM plan_items ORDER BY prefix').all(),
      edits: raw.prepare('SELECT tbl, pk, action FROM admin_edits ORDER BY id').all(),
    })
    it('no open plan yet: nothing is created', async () => {
      const { db, raw } = await sqliteD1(lineage)
      raw.exec(boom)
      await expect(stageItems(db, ['gs://b/a/', 'gs://b/b/', 'gs://b/boom/', 'gs://b/c/'], 'ann', 'memo', GS, '2026-10-07')).rejects.toThrow(/boom/)
      expect(dump(raw)).toEqual({ plans: [], batches: [], items: [], edits: [] })
    })
    it('an open plan: its items, batches and audit trail are untouched (absorbed items stay)', async () => {
      const { db, raw } = await sqliteD1(lineage)
      const first = await stageItems(db, ['gs://b/x/y/'], 'ann', 'first', GS, '2026-10-06')
      const before = dump(raw)
      raw.exec(boom)
      await expect(stageItems(db, ['gs://b/x/', 'gs://b/a/', 'gs://b/boom/'], 'bob', 'second', GS, '2026-10-07')).rejects.toThrow(/boom/)
      expect([first, before.items, dump(raw)]).toEqual([
        { plan_id: 1, batch_id: 1, staged: ['gs://b/x/y/'], covered: [], absorbed: [], as_of: '2026-10-06' },
        [{ plan_id: 1, prefix: 'gs://b/x/y/', batch_id: 1 }],
        before,
      ])
    })
    it('a gesture that stages nothing new writes no batch and no audit row', async () => {
      const { db, raw } = await sqliteD1(lineage)
      await stageItems(db, ['gs://b/x/'], 'ann', null, GS, '2026-10-06')
      const before = dump(raw)
      expect([
        await stageItems(db, ['gs://b/x/'], 'ann', null, GS, '2026-10-07'),
        await stageItems(db, ['gs://b/x/y/'], 'ann', null, GS, '2026-10-07'),
        dump(raw),
      ]).toEqual([
        { plan_id: 1, batch_id: null, staged: [], covered: [], absorbed: [], as_of: '2026-10-07' },
        { plan_id: 1, batch_id: null, staged: [], covered: ['gs://b/x/y/'], absorbed: [], as_of: '2026-10-07' },
        before,
      ])
    })
  })

  describe('mergeBatches — a chunked staging folds into one batch', () => {
    const GS = prefixShape({ STORE_SCHEME: 'gs://', STORE_BUCKETS: 'b' })!
    const dump = (raw: Sqlite) => ({
      batches: raw.prepare('SELECT id, note, created_by, created_ts FROM stage_batches ORDER BY id').all(),
      items: raw.prepare('SELECT prefix, batch_id, as_of FROM plan_items ORDER BY prefix').all(),
    })
    it('moves the items, keeps the earliest time, takes the new note, deletes the folded batches', async () => {
      const { db, raw } = await sqliteD1(lineage)
      await stageItems(db, ['gs://b/r/1/', 'gs://b/r/2/'], 'ann', 'marks (1/3)', GS, '2026-10-06')
      await stageItems(db, ['gs://b/r/3/'], 'ann', 'marks (2/3)', GS, '2026-10-07')
      await stageItems(db, ['gs://b/r/4/'], 'Ann', 'marks (3/3)', GS, '2026-10-07')
      await stageItems(db, ['gs://b/z/'], 'bob', 'other', GS, '2026-10-07')
      raw.exec('UPDATE stage_batches SET created_ts = 100 * id')
      expect(await mergeBatches(db, 1, 2, [1, 3, 2], 'marks', 'ann')).toEqual({ into: 2, merged: [1, 3], items: 3 })
      expect(dump(raw)).toEqual({
        batches: [
          { id: 2, note: 'marks', created_by: 'ann', created_ts: 100 },
          { id: 4, note: 'other', created_by: 'bob', created_ts: 400 },
        ],
        items: [
          { prefix: 'gs://b/r/1/', batch_id: 2, as_of: '2026-10-06' },
          { prefix: 'gs://b/r/2/', batch_id: 2, as_of: '2026-10-06' },
          { prefix: 'gs://b/r/3/', batch_id: 2, as_of: '2026-10-07' },
          { prefix: 'gs://b/r/4/', batch_id: 2, as_of: '2026-10-07' },
          { prefix: 'gs://b/z/', batch_id: 4, as_of: '2026-10-07' },
        ],
      })
    })
    it('refuses mixed stagers, foreign or missing batches, and a closed plan — writing nothing', async () => {
      const { db, raw } = await sqliteD1(lineage)
      await stageItems(db, ['gs://b/a/'], 'ann', null, GS, null)
      await stageItems(db, ['gs://b/c/'], 'bob', null, GS, null)
      const before = dump(raw)
      expect([
        await mergeBatches(db, 1, 1, [2], null, 'ann'),
        await mergeBatches(db, 1, 1, [9], null, 'ann'),
        await mergeBatches(db, 1, 1, [1], null, 'ann'),
      ]).toEqual([
        { error: 'batches were staged by different people (ann, bob)', status: 409 },
        { error: 'not batches of plan 1: 9', status: 404 },
        { error: 'ids: at least one batch other than `into`', status: 400 },
      ])
      raw.exec("UPDATE plans SET state = 'closed'")
      expect(await mergeBatches(db, 1, 1, [2], null, 'ann')).toEqual({ error: 'plan is closed', status: 409 })
      expect(dump(raw)).toEqual(before)
    })
    it('emptiedBatches replays the merge: a later ancestor absorbs the merged batch', async () => {
      const { db } = await sqliteD1(lineage)
      await stageItems(db, ['gs://b/r/1/'], 'ann', null, GS, null)
      await stageItems(db, ['gs://b/r/2/'], 'ann', null, GS, null)
      await mergeBatches(db, 1, 1, [2], null, 'ann')
      await stageItems(db, ['gs://b/r/'], 'bob', null, GS, null)
      const d = (await planDetail(db, 1, true))!
      expect(d.emptied.map(({ id, staged, absorbed, unstaged }) => ({ id, staged, absorbed, unstaged }))).toEqual([
        { id: 1, staged: 2, absorbed: [{ into: 3, n: 2 }], unstaged: 0 },
      ])
      expect(emptiedBatches(d.batches, d.items, [])).toEqual([{ ...d.emptied[0], staged: 0, absorbed: [] }])
    })
  })
})
