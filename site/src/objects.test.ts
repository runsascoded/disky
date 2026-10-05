import { describe, expect, it } from 'vitest'
import { actionPrefix, cellAction, filesRedirect, listsObjects, objectSource, openHref, publicUrl, rowTarget, type CellNode } from './objects'
import { resolveStores } from './stores'
import { TEST_REGISTRY } from './testStores'

// Objects as first-class nodes (specs/path-store.md §3): the click / link /
// open rules, where an object's bytes are read from per store, and where a
// retired `/files/…` link lands.

describe('listsObjects', () => {
  it('a store generation’s sorts list objects; a v1 index’s tiers (and no tier) do not', () => {
    const tiers = ['bysize', 'path', 'bysize-user', 'user', 'bysize+path', 'path+bysize', 'coarse20', 'fine', 'coarse24+fine', 'none', '', undefined]
    expect(tiers.map(t => [t, listsObjects(t)])).toEqual([
      ['bysize', true],
      ['path', true],
      ['bysize-user', true],
      ['user', true],
      ['bysize+path', true],
      ['path+bysize', true],
      ['coarse20', false],
      ['fine', false],
      ['coarse24+fine', false],
      ['none', false],
      ['', false],
      [undefined, false],
    ])
  })
})

describe('cellAction', () => {
  const cells: [string, CellNode][] = [
    ['object', { n: 'part-0.parquet', k: 'file', o: 1 }],
    ['dir with children', { n: 'ckpt', k: 'dir', o: 40, c: [{}] }],
    ['childless dir, many objects', { n: 'logs', k: 'dir', o: 12 }],
    ['childless dir, one object', { n: 'one', k: 'dir', o: 1 }],
    ['fold', { n: '(other)', k: 'dir', o: 300, c: [] }],
    ['pre-`k` response', { n: 'old', o: 1 }],
  ]
  it('a scan that lists objects: an object opens, every directory drills (childless or not), a fold pins', () => {
    expect(cells.map(([what, n]) => [what, cellAction(n, true)])).toEqual([
      ['object', 'open'],
      ['dir with children', 'drill'],
      ['childless dir, many objects', 'drill'],
      ['childless dir, one object', 'drill'],
      ['fold', 'pin'],
      ['pre-`k` response', 'drill'],
    ])
  })
  it('a v1 scan (every leaf a directory): as before — a childless directory of ≤ 1 object pins, since drilling there draws nothing', () => {
    expect(cells.map(([what, n]) => [what, cellAction(n, false)])).toEqual([
      ['object', 'open'],
      ['dir with children', 'drill'],
      ['childless dir, many objects', 'drill'],
      ['childless dir, one object', 'pin'],
      ['fold', 'pin'],
      ['pre-`k` response', 'pin'],
    ])
  })
  it('⌥-click pins any cell, on either generation', () => {
    expect(cells.map(([, n]) => [cellAction(n, true, true), cellAction(n, false, true)])).toEqual(cells.map(() => ['pin', 'pin']))
  })
})

describe('rowTarget', () => {
  it('an object row opens its path, a directory row drills to it, a fold is not a link', () => {
    expect([
      rowTarget(['ctbk', 'gbfs'], 'status.parquet', 'file'),
      rowTarget(['ctbk', 'gbfs'], 'status', 'dir'),
      rowTarget(['ctbk', 'gbfs'], 'leaf', undefined),
      rowTarget(['ctbk', 'gbfs'], '(other)', 'dir'),
      rowTarget([], 'ctbk', 'dir'),
    ]).toEqual([
      { kind: 'open', segs: ['ctbk', 'gbfs', 'status.parquet'] },
      { kind: 'drill', segs: ['ctbk', 'gbfs', 'status'] },
      { kind: 'drill', segs: ['ctbk', 'gbfs', 'leaf'] },
      null,
      { kind: 'drill', segs: ['ctbk'] },
    ])
  })
})

describe('openHref', () => {
  it('the object’s directory is the drill path, its basename `open`; the page’s other params stay', () => {
    expect(openHref('/', ['ctbk', 'gbfs', 'a b.parquet'], '?d=260930&c=w')).toEqual({ pathname: '/ctbk/gbfs', search: '?d=260930&c=w&open=a+b.parquet' })
    expect(openHref('/', ['jc-taxes', 'x.json'], '?open=y.json')).toEqual({ pathname: '/jc-taxes', search: '?open=x.json' })
    expect(openHref('/meta', ['my-data', 'listing', 'k.parquet'], '')).toEqual({ pathname: '/meta/my-data/listing', search: '?open=k.parquet' })
  })
})

describe('objectSource', () => {
  const [r2] = resolveStores('r2', '', TEST_REGISTRY)
  const [cw, meta] = resolveStores('cw', 'meta', TEST_REGISTRY)
  const [gcs] = resolveStores('gcs', '', TEST_REGISTRY)
  const [laptop] = resolveStores('laptop', '', TEST_REGISTRY)
  it('r2: a bucket with a public domain is read from it; jc-taxes (its CORS admits only jct.rbw.sh) is not, and the proxy reads the index bucket', () => {
    const proxy = { uri: 'r2://disk-tree-demo', prefixes: ['listing/', 'snapshots/', 'sweep/'] }
    expect([
      objectSource(r2, ['ctbk', 'gbfs', 'status', '2026-08-24.parquet'], proxy),
      objectSource(r2, ['crashes', 'njdot', 'data', '2007', 'NewJersey2007Accidents.pqt'], proxy),
      objectSource(r2, ['jc-taxes', 'data', 'records', 'payments.parquet'], proxy),
    ]).toEqual([
      { kind: 'public', base: 'https://data.ctbk.dev', key: 'gbfs/status/2026-08-24.parquet' },
      { kind: 'public', base: 'https://crashes-data.hccs.dev', key: 'njdot/data/2007/NewJersey2007Accidents.pqt' },
      { kind: 'none', bucket: 'jc-taxes', key: 'data/records/payments.parquet' },
    ])
  })
  it('cw / gcs: the scanned buckets are neither public nor the proxy’s, so size and dates only', () => {
    expect([
      objectSource(cw, ['data-a', 'ckpt', 'model.safetensors'], { uri: 'r2://my-index', prefixes: ['listing/', 'cw-l2/'] }),
      objectSource(gcs, ['bucket-1', 'tokenized', 'x.jsonl.gz'], { uri: 'gs://my-data', prefixes: ['listing/', 'snapshots/', 'sweep/'] }),
      objectSource(laptop, ['Users', 'ryan', 'notes.md'], { uri: 'gs://my-data', prefixes: ['listing/'] }),
    ]).toEqual([
      { kind: 'none', bucket: 'data-a', key: 'ckpt/model.safetensors' },
      { kind: 'none', bucket: 'bucket-1', key: 'tokenized/x.jsonl.gz' },
      { kind: 'none', bucket: 'Users', key: 'ryan/notes.md' },
    ])
  })
  it('gcs: a scanned bucket the deployment serves to members reads through `/v1/objects/<bucket>`; others, and a guest (no `objectBuckets`), get size and dates', () => {
    const proxy = { uri: 'gs://my-data', prefixes: ['listing/', 'snapshots/', 'sweep/'] }
    const member = { ...proxy, objectBuckets: ['bucket-1', 'bucket-2'] }
    expect([
      objectSource(gcs, ['bucket-1', 'tokenized', 'x.jsonl.gz'], member),
      objectSource(gcs, ['bucket-3', 'raw', 'y.parquet'], member),
      objectSource(gcs, ['bucket-1', 'tokenized', 'x.jsonl.gz'], proxy),
      objectSource(gcs, ['my-data', 'snapshots', 'rules.json'], member),
    ]).toEqual([
      { kind: 'proxy', key: 'tokenized/x.jsonl.gz', api: '/v1/objects/bucket-1' },
      { kind: 'none', bucket: 'bucket-3', key: 'raw/y.parquet' },
      { kind: 'none', bucket: 'bucket-1', key: 'tokenized/x.jsonl.gz' },
      { kind: 'proxy', key: 'snapshots/rules.json', api: '/v1/files' },
    ])
  })
  it('meta: the proxy reads the object’s bucket — under an allowed prefix only', () => {
    const proxy = { uri: 'gs://my-data', prefixes: ['meta-l2/', 'snapshots/meta/'] }
    expect([
      objectSource(meta, ['my-data', 'snapshots', 'meta', '2026-09-30', 'meta.json'], proxy),
      objectSource(meta, ['my-data', 'listing', '2026-09-30', 'x.parquet'], proxy),
      objectSource(meta, ['my-index', 'meta-l2', 'a.parquet'], proxy),
      objectSource(meta, ['my-data', 'meta-l2', 'a.parquet'], null),
    ]).toEqual([
      { kind: 'proxy', key: 'snapshots/meta/2026-09-30/meta.json', api: '/v1/files' },
      { kind: 'none', bucket: 'my-data', key: 'listing/2026-09-30/x.parquet' },
      { kind: 'none', bucket: 'my-index', key: 'meta-l2/a.parquet' },
      { kind: 'none', bucket: 'my-data', key: 'meta-l2/a.parquet' },
    ])
  })
  it('a public URL percent-encodes each key segment and keeps the slashes', () => {
    expect(publicUrl('https://data.ctbk.dev', 'a b/c#d/e.parquet')).toBe('https://data.ctbk.dev/a%20b/c%23d/e.parquet')
  })
})

describe('filesRedirect', () => {
  const stores = resolveStores('gcs', 'meta', TEST_REGISTRY)
  const [gcs] = stores
  const proxy = { uri: 'gs://my-data', prefixes: ['listing/', 'snapshots/', 'sweep/'] }
  it('a store that scans the proxy’s bucket shows the key: a directory drills there, an object opens under its directory', () => {
    expect([
      filesRedirect('', proxy, stores, gcs),
      filesRedirect('listing/2026-09-30/', proxy, stores, gcs),
      filesRedirect('listing/2026-09-30/bucket-1/part-0.parquet', proxy, stores, gcs),
      filesRedirect('sweep/runs/a%20b.json', proxy, stores, gcs),
    ]).toEqual([
      { pathname: '/meta/my-data', search: '' },
      { pathname: '/meta/my-data/listing/2026-09-30', search: '' },
      { pathname: '/meta/my-data/listing/2026-09-30/bucket-1', search: '?open=part-0.parquet' },
      { pathname: '/meta/my-data/sweep/runs', search: '?open=a+b.json' },
    ])
  })
  it('no configured store scans the proxy’s bucket (r2.rbw.sh), or the proxy is unknown: the store root', () => {
    const [r2] = resolveStores('r2', '', TEST_REGISTRY)
    expect([
      filesRedirect('listing/2026-09-30/', { uri: 'r2://disk-tree-demo', prefixes: ['listing/'] }, [r2], r2),
      filesRedirect('listing/x.parquet', null, stores, gcs),
      filesRedirect('listing/x.parquet', proxy, [gcs], gcs),
    ]).toEqual([
      { pathname: '/', search: '' },
      { pathname: '/', search: '' },
      { pathname: '/', search: '' },
    ])
  })
})

describe('actionPrefix', () => {
  it('an object stages / assigns by its key, a directory by its `/`-terminated prefix', () => {
    expect([actionPrefix('r2://ctbk/a.parquet', 'file'), actionPrefix('r2://ctbk/gbfs', 'dir'), actionPrefix('r2://ctbk/gbfs', undefined)])
      .toEqual(['r2://ctbk/a.parquet', 'r2://ctbk/gbfs/', 'r2://ctbk/gbfs/'])
  })
})
