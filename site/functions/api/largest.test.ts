import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { openIndex, readLargest } from '../_lib/index'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, readJson, seedGeneration } from '../_lib/testStore'
import { onRequestGet as largest } from './largest'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// The largest files under a path, from the `bysize` sort of the v2 fixture
// (`fixtures/gen.py`): bucket `bk` = `flat/f00000..f07999` (size `2^(i % 16)`),
// `small/s0..s2` (1000, 2000, 3000 B), `nest/a/b/c0..c3` (1 MiB each),
// `nest/a/z` (256 KiB), `empty.bin` (0 B), plus their dirs. The size sort's
// four 2048-row groups, largest bucket first:
//   0: b_max 37229948 (the dirs, the nest files, flat's 2^15..2^12, part of 2^11), paths bk..bk/small
//   1: b_max 3000, paths bk/flat/f00007..bk/small/s2
//   2: b_max 128, paths bk/flat/f00003..bk/flat/f07991
//   3: b_max 8, paths bk/empty.bin..bk/flat/f07987
// Served three ways: groups in D1, retired to the `.groups.json` blob, and
// retired with cold `.groups.parquet` footers.

describe('D1', () => {
  const D1 = '2026-09-30T0001'
  const BLOB = '2026-09-30T0002'
  const PQ = '2026-09-30T0005'
  const MiB = 1 << 20
  const MTIME = 1788220800
  let env: Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    const { db, raw } = await sqliteD1()
    const v2 = await readJson<Record<string, D1Variant>>('v2/d1.json')
    const files = { path: { parquet: 'v2/path-index.parquet', groups: 'v2/path-index.groups.json' }, bysize: { parquet: 'v2/path-index-bysize.parquet', groups: 'v2/path-index-bysize.groups.json' } }
    const pqFiles = { path: { ...files.path, footer: 'v2/path-index.groups.parquet' }, bysize: { ...files.bysize, footer: 'v2/path-index-bysize.groups.parquet' } }
    seedGeneration(raw, { date: D1, gen: 'g2', dir: `cw-l2/${D1}/index/g2`, variants: v2, files })
    seedGeneration(raw, { date: BLOB, gen: 'g3', dir: `cw-l2/${BLOB}/index/g3`, variants: v2, files, retired: ['path', 'bysize'] })
    seedGeneration(raw, { date: PQ, gen: 'g6', dir: `cw-l2/${PQ}/index/g6`, variants: v2, files: pqFiles, retired: ['path', 'bysize'] })
    env = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
  })

  type F = [path: string, size: number]
  /** Every object in the fixture, by the generator's rule. */
  const ALL: F[] = [
    ...Array.from({ length: 8000 }, (_, i): F => [`bk/flat/f${String(i).padStart(5, '0')}`, 1 << (i % 16)]),
    ...[0, 1, 2].map((k): F => [`bk/small/s${k}`, 1000 * (k + 1)]),
    ...[0, 1, 2, 3].map((k): F => [`bk/nest/a/b/c${k}`, MiB]),
    ['bk/nest/a/z', MiB / 4],
    ['bk/empty.bin', 0],
  ]
  /** The spec: objects under `P`, size-descending, ties by path. */
  const expected = (P: string, n: number): F[] => ALL
    .filter(([p]) => P === '' || p.startsWith(P + '/'))
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
    .slice(0, n)

  const run = async (date: string, P: string, n: number, maxGroups?: number) => {
    const r = await readLargest(await openIndex(env, date, 'bysize'), P, n, maxGroups)
    return { files: r.rows.map((x): F => [x.path, x.size]), groups: r.groups, decoded: r.decoded, more: r.more, capped: r.capped }
  }

  type D = [path: string, size: number, nFiles: number]
  const flatBytes = 500 * 65535
  const nestBytes = 4 * MiB + MiB / 4
  /** Every directory in the fixture, with its bytes and the objects below it. */
  const DIRS: D[] = [
    ['bk', flatBytes + 6000 + nestBytes, 8009],
    ['bk/flat', flatBytes, 8000],
    ['bk/nest', nestBytes, 5],
    ['bk/nest/a', nestBytes, 5],
    ['bk/nest/a/b', 4 * MiB, 4],
    ['bk/small', 6000, 3],
  ]
  /** The spec: dirs strictly below `P`, size-descending, ties by path. */
  const expectedDirs = (P: string, n: number): D[] => DIRS
    .filter(([p]) => P === '' || p.startsWith(P + '/'))
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
    .slice(0, n)
  const runDirs = async (date: string, P: string, n: number, maxGroups?: number) => {
    const r = await readLargest(await openIndex(env, date, 'bysize'), P, n, maxGroups, 'dir')
    return { dirs: r.rows.map((x): D => [x.path, x.size, x.n_files]), groups: r.groups, decoded: r.decoded, more: r.more, capped: r.capped }
  }

  describe('readLargest', () => {
    for (const date of [D1, BLOB, PQ]) {
      describe(date, () => {
        it('the top of a deep subtree: one group of four decoded, the rest skipped by bucket', async () => {
          const want = [['bk/nest/a/b/c0', MiB], ['bk/nest/a/b/c1', MiB], ['bk/nest/a/b/c2', MiB], ['bk/nest/a/b/c3', MiB], ['bk/nest/a/z', MiB / 4]]
          expect(await run(date, 'bk', 5)).toEqual({ files: want, groups: [0], decoded: 2048, more: true, capped: false })
          // The store root reads the same group.
          expect(await run(date, '', 5)).toEqual({ files: want, groups: [0], decoded: 2048, more: true, capped: false })
          expect(expected('bk', 5)).toEqual(want)
        })

        it('ties at one size break by path; a 500-file answer still stops after group 0', async () => {
          expect(await run(date, 'bk/flat', 3)).toEqual({ files: [['bk/flat/f00015', 32768], ['bk/flat/f00031', 32768], ['bk/flat/f00047', 32768]], groups: [0], decoded: 2048, more: true, capped: false })
          expect(await run(date, 'bk/flat', 500)).toEqual({ files: expected('bk/flat', 500), groups: [0], decoded: 2048, more: true, capped: false })
        })

        it('a subtree whose files sit in a later group: the span query prunes the groups outside its path range', async () => {
          expect(await run(date, 'bk/small', 10)).toEqual({ files: [['bk/small/s2', 3000], ['bk/small/s1', 2000], ['bk/small/s0', 1000]], groups: [1], decoded: 2048, more: false, capped: false })
          // Fewer files than asked: every group whose path range meets it is read (group 1's straddles `bk/nest/a/`).
          expect(await run(date, 'bk/nest/a', 10)).toEqual({ files: expected('bk/nest/a', 10), groups: [0, 1], decoded: 4096, more: false, capped: false })
          // Exactly as many as asked: it stops after group 0, so `more` says there may be more (group 1 is unread).
          expect(await run(date, 'bk/nest/a', 5)).toEqual({ files: expected('bk/nest/a', 5), groups: [0], decoded: 2048, more: true, capped: false })
        })

        it('asked for everything, it reads every group and returns every object, never a dir, the 0 B one last', async () => {
          const all = await run(date, '', 10_000)
          expect(all).toEqual({ files: expected('', 10_000), groups: [0, 1, 2, 3], decoded: 8015, more: false, capped: false })
          expect([all.files.length, all.files[all.files.length - 1]]).toEqual([8009, ['bk/empty.bin', 0]])
        })

        it('a path outside the store, or an object (nothing under it), lists nothing', async () => {
          expect(await run(date, 'zz', 5)).toEqual({ files: [], groups: [], decoded: 0, more: false, capped: false })
          // An object's range `[P/, P0)` still meets the groups whose path ranges straddle it: they are read, and hold nothing.
          expect(await run(date, 'bk/nest/a/z', 5)).toEqual({ files: [], groups: [0, 1], decoded: 4096, more: false, capped: false })
        })

        it('holds to its group budget: the largest of what it read, flagged', async () => {
          // Groups 0 and 1 hold every object over 128 B and the first 128 B ones by path (all six dirs are in group 0).
          expect(await run(date, '', 10_000, 2)).toEqual({ files: expected('', 4090), groups: [0, 1], decoded: 4096, more: true, capped: true })
          expect(await run(date, '', 10_000, 0)).toEqual({ files: [], groups: [], decoded: 0, more: true, capped: true })
        })
      })
    }
  })

  describe('readLargest, kind dir', () => {
    for (const date of [D1, BLOB, PQ]) {
      describe(date, () => {
        it('the largest folders below a path, never the path itself nor an object', async () => {
          expect(await runDirs(date, 'bk', 2)).toEqual({ dirs: [['bk/flat', flatBytes, 8000], ['bk/nest', nestBytes, 5]], groups: [0], decoded: 2048, more: true, capped: false })
          expect(await runDirs(date, 'bk/nest', 10)).toEqual({ dirs: [['bk/nest/a', nestBytes, 5], ['bk/nest/a/b', 4 * MiB, 4]], groups: [0, 1], decoded: 4096, more: false, capped: false })
          expect(expectedDirs('bk', 2)).toEqual([['bk/flat', flatBytes, 8000], ['bk/nest', nestBytes, 5]])
        })

        it('at the store root: every dir (the bucket too), largest first; fewer than asked reads every group', async () => {
          expect(await runDirs(date, '', 10)).toEqual({ dirs: expectedDirs('', 10), groups: [0, 1, 2, 3], decoded: 8015, more: false, capped: false })
          expect(expectedDirs('', 10).map(d => d[0])).toEqual(['bk', 'bk/flat', 'bk/nest', 'bk/nest/a', 'bk/nest/a/b', 'bk/small'])
        })

        it('a leaf folder, an object or a path outside the store: no folders below it', async () => {
          expect(await Promise.all([runDirs(date, 'bk/small', 5), runDirs(date, 'bk/nest/a/b', 5), runDirs(date, 'zz', 5)])).toEqual([
            { dirs: [], groups: [1], decoded: 2048, more: false, capped: false },
            { dirs: [], groups: [0, 1], decoded: 4096, more: false, capped: false },
            { dirs: [], groups: [], decoded: 0, more: false, capped: false },
          ])
        })
      })
    }
  })

  describe('GET /api/largest', () => {
    const get = async (qs: string) => {
      const r = await largest({ request: new Request(`http://localhost/api/largest?${qs}`), env })
      return { status: r.status, body: r.headers.get('content-type')?.startsWith('application/json') ? await r.json() : await r.text() }
    }

    it('answers the files with their stamps and the read cost', async () => {
      expect(await get(`date=${D1}&path=bk/nest&n=2`)).toEqual({
        status: 200,
        body: { date: D1, path: 'bk/nest', n: 2, files: [{ path: 'bk/nest/a/b/c0', size: MiB, mtime: MTIME }, { path: 'bk/nest/a/b/c1', size: MiB, mtime: MTIME }], more: true, groups: 1, rows: 2048 },
      })
    })

    it('a trailing slash is the same path; `n` defaults to 100', async () => {
      const r = await get(`date=${D1}&path=bk/small/`)
      expect(r).toEqual({
        status: 200,
        body: { date: D1, path: 'bk/small', n: 100, files: [{ path: 'bk/small/s2', size: 3000, mtime: MTIME }, { path: 'bk/small/s1', size: 2000, mtime: MTIME }, { path: 'bk/small/s0', size: 1000, mtime: MTIME }], more: false, groups: 1, rows: 2048 },
      })
    })

    it('`kind=dir`: the largest folders below, with their object counts', async () => {
      expect(await get(`date=${D1}&path=bk&n=3&kind=dir`)).toEqual({
        status: 200,
        body: {
          date: D1, path: 'bk', n: 3, kind: 'dir',
          dirs: [
            { path: 'bk/flat', size: flatBytes, mtime: MTIME, n_files: 8000 },
            { path: 'bk/nest', size: nestBytes, mtime: MTIME, n_files: 5 },
            { path: 'bk/nest/a', size: nestBytes, mtime: MTIME, n_files: 5 },
          ],
          more: true, groups: 1, rows: 2048,
        },
      })
      // `kind=file` is the default's answer.
      expect(await get(`date=${D1}&path=bk/small&kind=file`)).toEqual(await get(`date=${D1}&path=bk/small`))
    })

    it('refuses a bad n, path or scan', async () => {
      expect(await Promise.all([
        get(`date=${D1}&path=bk&n=0`),
        get(`date=${D1}&path=bk&n=501`),
        get(`date=${D1}&path=bk&n=2.5`),
        get(`date=${D1}&path=/bk`),
        get(`date=${D1}&path=bk/../x`),
        get('path=bk'),
        get('date=2026-01-01&path=bk'),
        get(`date=${D1}&path=bk&kind=dirs`),
      ])).toEqual([
        { status: 400, body: 'bad n (want 1..500)' },
        { status: 400, body: 'bad n (want 1..500)' },
        { status: 400, body: 'bad n (want 1..500)' },
        { status: 400, body: 'bad path' },
        { status: 400, body: 'bad path' },
        { status: 400, body: 'bad date' },
        { status: 404, body: { error: 'no scan matches date=2026-01-01' } },
        { status: 400, body: 'bad kind (want file|dir)' },
      ])
    })
  })
})
