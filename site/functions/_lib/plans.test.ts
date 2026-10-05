import { describe, expect, it } from 'vitest'
import { bucketOf, canonicalPrefix, covers, planBucket, planDigest, PlanSpansBuckets, planStaging, prefixShape, realGate, relPrefix, type RunRow, uncovered } from './plans'

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
