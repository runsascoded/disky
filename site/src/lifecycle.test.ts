import { describe, expect, it } from 'vitest'
import { describeRule, displayId, fromGcs, groupRows, lifecycleDiff, lifecycleDiffByBucket, parseLifecycle, rulePrefix } from './lifecycle'
import type { BucketLifecycleRow, GcsRule, GroupedRow, LifecycleRule, LifecycleRow, LifecycleSnapshot } from './lifecycle'

const ttl = (days: number): LifecycleRule => ({ ID: `marin-ttl-${days}d`, Filter: { Prefix: `tmp/ttl=${days}d/` }, Status: 'Enabled', Expiration: { Days: days } })
const gc: LifecycleRule = { ID: 'cw-noncurrent-gc', Filter: { Prefix: '' }, Status: 'Enabled', NoncurrentVersionExpiration: { NoncurrentDays: 1 }, Expiration: { ExpiredObjectDeleteMarker: true } }
const mpu: LifecycleRule = { ID: 'marin-abort-incomplete-mpu', Filter: { Prefix: '' }, Status: 'Enabled', AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 } }
const test: LifecycleRule = { ID: 'cw-sweep-lifecycle-test', Filter: { Prefix: 'tmp/cw-sweep-lifecycle-test/' }, Status: 'Enabled', NoncurrentVersionExpiration: { NoncurrentDays: 1 }, Expiration: { ExpiredObjectDeleteMarker: true } }

describe('describeRule', () => {
  it('words each action', () => {
    expect(describeRule(ttl(14))).toBe('expire objects after 14 d')
    expect(describeRule(gc)).toBe('expire noncurrent versions after 1 d + drop expired delete markers')
    expect(describeRule(mpu)).toBe('abort incomplete multipart uploads after 7 d')
    expect(describeRule({ ID: 'x', Status: 'Disabled' })).toBe('no action')
  })
  it('joins every action a rule carries, objects first', () => {
    expect(describeRule({ ...gc, Expiration: { Days: 3, ExpiredObjectDeleteMarker: true }, AbortIncompleteMultipartUpload: { DaysAfterInitiation: 2 } }))
      .toBe('expire objects after 3 d + expire noncurrent versions after 1 d + drop expired delete markers + abort incomplete multipart uploads after 2 d')
  })
  it('prefix: empty filter = whole bucket', () => {
    expect(rulePrefix(gc)).toBe('')
    expect(rulePrefix({ ID: 'x', Status: 'Enabled' })).toBe('')
    expect(rulePrefix(ttl(1))).toBe('tmp/ttl=1d/')
  })
})

describe('lifecycleDiff', () => {
  it('no previous snapshot: rows sorted by ID (numeric-aware), no changes', () => {
    expect(lifecycleDiff(null, [ttl(7), gc, ttl(14), ttl(1)])).toEqual<LifecycleRow[]>([
      { rule: gc, change: null },
      { rule: ttl(1), change: null },
      { rule: ttl(7), change: null },
      { rule: ttl(14), change: null },
    ])
  })
  it('added, removed, changed against the previous snapshot', () => {
    const prev = [mpu, ttl(1), test, { ...ttl(7), Status: 'Disabled' }]
    const cur = [gc, mpu, ttl(1), ttl(7)]
    expect(lifecycleDiff(prev, cur)).toEqual<LifecycleRow[]>([
      { rule: gc, change: 'new' },
      { rule: mpu, change: null },
      { rule: ttl(1), change: null },
      { rule: ttl(7), change: 'changed', prev: { ...ttl(7), Status: 'Disabled' } },
      { rule: test, change: 'removed' },
    ])
  })
  it('identical snapshots: no chips', () => {
    const rules = [mpu, ttl(1), ttl(14)]
    expect(lifecycleDiff(rules, rules).map(r => r.change)).toEqual([null, null, null])
  })
  it('a changed action (not just status) is `changed`, with the old rule kept', () => {
    const was = { ...ttl(2), Expiration: { Days: 3 } }
    expect(lifecycleDiff([was], [ttl(2)])).toEqual<LifecycleRow[]>([{ rule: ttl(2), change: 'changed', prev: was }])
  })
})

describe('parseLifecycle', () => {
  it('a bare Rules[] (scans before the multi-bucket job) is the primary bucket’s', () => {
    expect(parseLifecycle([ttl(1), mpu], 'marin-us-east-02a')).toEqual<LifecycleSnapshot>({ 'marin-us-east-02a': [ttl(1), mpu] })
  })
  it('a {bucket: Rules[]} map is taken as is', () => {
    const snap: LifecycleSnapshot = { 'marin-us-east-02a': [gc, mpu], 'hero-checkpoints': [mpu] }
    expect(parseLifecycle(snap, 'marin-us-east-02a')).toEqual(snap)
  })
  it('anything else throws', () => {
    expect(() => parseLifecycle('x', 'b')).toThrow('lifecycle.json: expected Rules[] or {bucket: Rules[]}')
    expect(() => parseLifecycle(null, 'b')).toThrow('lifecycle.json: expected Rules[] or {bucket: Rules[]}')
    expect(() => parseLifecycle({ b: 'x' }, 'b')).toThrow('lifecycle.json: expected Rules[] or {bucket: Rules[]}')
  })
  it('a rule of the other cloud’s shape is a mis-configured store, not a row', () => {
    expect(() => parseLifecycle([gTtl(1)], 'b', 's3')).toThrow('lifecycle.json: expected an S3 rule with an ID')
    expect(() => parseLifecycle([ttl(1)], 'b', 'gcs')).toThrow('lifecycle.json: expected a GCS {action, condition} rule')
  })
})

describe('lifecycleDiffByBucket', () => {
  const P = 'marin-us-east-02a'
  const H = 'hero-checkpoints'
  it('diffs each bucket by rule ID within the bucket, in the current snapshot’s order', () => {
    const prev: LifecycleSnapshot = { [P]: [mpu, ttl(1)], [H]: [ttl(1)] }
    const cur: LifecycleSnapshot = { [P]: [mpu, ttl(1), gc], [H]: [{ ...ttl(1), Expiration: { Days: 2 } }] }
    expect(lifecycleDiffByBucket(prev, cur)).toEqual<BucketLifecycleRow[]>([
      { bucket: P, rule: gc, change: 'new' },
      { bucket: P, rule: mpu, change: null },
      { bucket: P, rule: ttl(1), change: null },
      { bucket: H, rule: { ...ttl(1), Expiration: { Days: 2 } }, change: 'changed', prev: ttl(1) },
    ])
  })
  it('a bucket new to the snapshot is all `new`; one only the previous scan had is all `removed`', () => {
    expect(lifecycleDiffByBucket({ [P]: [mpu] }, { [P]: [mpu], [H]: [ttl(7), ttl(1)] })).toEqual<BucketLifecycleRow[]>([
      { bucket: P, rule: mpu, change: null },
      { bucket: H, rule: ttl(1), change: 'new' },
      { bucket: H, rule: ttl(7), change: 'new' },
    ])
    expect(lifecycleDiffByBucket({ [P]: [mpu], [H]: [ttl(7), ttl(1)] }, { [P]: [mpu] })).toEqual<BucketLifecycleRow[]>([
      { bucket: P, rule: mpu, change: null },
      { bucket: H, rule: ttl(1), change: 'removed' },
      { bucket: H, rule: ttl(7), change: 'removed' },
    ])
  })
  it('no previous snapshot: every row unchanged', () => {
    expect(lifecycleDiffByBucket(null, { [H]: [ttl(1)] })).toEqual<BucketLifecycleRow[]>([{ bucket: H, rule: ttl(1), change: null }])
  })
})

// GCS rules: anonymous `{action, condition}` → S3-shaped rows with a content ID.
const gTtl = (days: number): GcsRule => ({ action: { type: 'Delete' }, condition: { age: days, matchesPrefix: [`tmp/ttl=${days}d/`] } })
const gCustom: GcsRule = { action: { type: 'Delete' }, condition: { daysSinceCustomTime: 0, matchesPrefix: ['scratch/compilation_cache/'] } }
const gCold: GcsRule = { action: { type: 'SetStorageClass', storageClass: 'COLDLINE' }, condition: { age: 90, matchesStorageClass: ['STANDARD'] } }
const gNoncurrent: GcsRule = { action: { type: 'Delete' }, condition: { isLive: false, numNewerVersions: 3 } }
const gMpu: GcsRule = { action: { type: 'AbortIncompleteMultipartUpload' }, condition: { age: 7 } }

describe('fromGcs', () => {
  it('maps a TTL rule onto Expiration + Filter, with a content ID', () => {
    expect(fromGcs(gTtl(14))).toEqual<LifecycleRule>({
      ID: 'delete age=14 matchesPrefix=tmp/ttl=14d/',
      Status: 'Enabled',
      Filter: { Prefix: 'tmp/ttl=14d/' },
      Expiration: { Days: 14 },
    })
    expect(describeRule(fromGcs(gTtl(14)))).toBe('expire objects after 14 d')
    expect(displayId(fromGcs(gTtl(14)))).toBe('delete age=14')
    expect(displayId(fromGcs(gNoncurrent))).toBe('delete isLive=false numNewerVersions=3')
    expect(displayId(ttl(14))).toBe('marin-ttl-14d')
  })
  it('words what S3 has no field for', () => {
    expect(fromGcs(gCustom)).toEqual<LifecycleRule>({
      ID: 'delete daysSinceCustomTime=0 matchesPrefix=scratch/compilation_cache/',
      Status: 'Enabled',
      Filter: { Prefix: 'scratch/compilation_cache/' },
      Extra: "0 d after the object's custom time",
    })
    expect(fromGcs(gCold)).toEqual<LifecycleRule>({
      ID: 'setstorageclass age=90 matchesStorageClass=STANDARD',
      Status: 'Enabled',
      Extra: 'move to COLDLINE after 90 d, in STANDARD',
    })
    expect(fromGcs(gNoncurrent)).toEqual<LifecycleRule>({
      ID: 'delete isLive=false numNewerVersions=3',
      Status: 'Enabled',
      Extra: 'when 3 newer versions exist',
    })
    expect(fromGcs(gMpu)).toEqual<LifecycleRule>({
      ID: 'abortincompletemultipartupload age=7',
      Status: 'Enabled',
      AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
    })
    expect(describeRule(fromGcs(gCustom))).toBe("0 d after the object's custom time")
  })
})

describe('parseLifecycle: a GCS store', () => {
  // `job/lifecycle/marin-us-central2.json` as the gcs job tracks it: the nine
  // Marin TTLs in the API's (lexical) order.
  const central2: GcsRule[] = [1, 14, 2, 3, 30, 4, 5, 6, 7].map(gTtl)
  it('a bare list is the primary bucket’s, each rule through the GCS adapter', () => {
    expect(parseLifecycle(central2, 'marin-us-central2', 'gcs')).toEqual<LifecycleSnapshot>({ 'marin-us-central2': central2.map(fromGcs) })
  })
  it('the rows sort by TTL days, not lexically', () => {
    const snap = parseLifecycle(central2, 'marin-us-central2', 'gcs')
    expect(lifecycleDiff(null, snap['marin-us-central2']).map(r => displayId(r.rule))).toEqual([
      'delete age=1', 'delete age=2', 'delete age=3', 'delete age=4', 'delete age=5', 'delete age=6', 'delete age=7', 'delete age=14', 'delete age=30',
    ])
  })
  it('a {bucket: rules} map converts every bucket', () => {
    expect(parseLifecycle({ 'marin-us-west4': [gTtl(1), gCustom], 'marin-eu-west4': [gTtl(1)] }, 'marin-us-west4', 'gcs')).toEqual<LifecycleSnapshot>({
      'marin-us-west4': [fromGcs(gTtl(1)), fromGcs(gCustom)],
      'marin-eu-west4': [fromGcs(gTtl(1))],
    })
  })
})

describe('groupRows', () => {
  const t1 = fromGcs(gTtl(1)); const t14 = fromGcs(gTtl(14)); const custom = fromGcs(gCustom)
  it('a fleet-wide rule shows once with every bucket; a one-bucket rule with its bucket', () => {
    const rows = lifecycleDiffByBucket(null, { 'marin-us-west4': [t1, t14, custom], 'marin-eu-west4': [t14, t1] })
    expect(groupRows(rows)).toEqual<GroupedRow[]>([
      { rule: t1, change: null, buckets: ['marin-eu-west4', 'marin-us-west4'] },
      { rule: t14, change: null, buckets: ['marin-eu-west4', 'marin-us-west4'] },
      { rule: custom, change: null, buckets: ['marin-us-west4'] },
    ])
  })
  it('the same rule with different changes stays two rows; removed rows sort last', () => {
    const rows = lifecycleDiffByBucket({ a: [t14, t1], b: [t1] }, { a: [t1], b: [t1, t14] })  // t14 removed on a, new on b
    expect(groupRows(rows)).toEqual<GroupedRow[]>([
      { rule: t1, change: null, buckets: ['a', 'b'] },
      { rule: t14, change: 'new', buckets: ['b'] },
      { rule: t14, change: 'removed', buckets: ['a'] },
    ])
  })
  it('the six-bucket fleet with identical TTLs: one row per TTL, all six buckets', () => {
    const fleet = ['marin-us-central2', 'marin-us-central1', 'marin-us-east1', 'marin-us-east5', 'marin-us-west4', 'marin-eu-west4']
    const snap = parseLifecycle(Object.fromEntries(fleet.map(b => [b, [1, 14, 2].map(gTtl)])), fleet[0], 'gcs')
    expect(groupRows(lifecycleDiffByBucket(snap, snap))).toEqual<GroupedRow[]>([
      { rule: t1, change: null, buckets: [...fleet].sort() },
      { rule: fromGcs(gTtl(2)), change: null, buckets: [...fleet].sort() },
      { rule: t14, change: null, buckets: [...fleet].sort() },
    ])
  })
})
