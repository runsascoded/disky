import { describe, expect, it } from 'vitest'
import { foldPrefixes } from './prefixes'

const row = (path: string, usr: string | null, size: number, o: number, extra: { mt?: number; read?: number; c3?: number; c4?: number } = {}) => ({
  path, depth: path.split('/').length, usr, size, n_files: o,
  mtime: extra.mt ?? null, last_read: extra.read ?? null,
  cls2: 0, cls3: extra.c3 ?? 0, cls4: extra.c4 ?? 0,
})

describe('foldPrefixes: index rows → one stat per staged prefix', () => {
  it('sums owner slices and class bytes, keeps the newest write and the latest read, drops rows nobody asked for', () => {
    const day = 86400
    const rows = [
      row('b/x', 'ann', 300, 3, { mt: 100 * day + 5, read: 7, c3: 100 }),
      row('b/x', 'bob', 100, 1, { mt: 200 * day + 5, read: 9, c4: 50 }),
      row('b/x', null, 600, 2, { c3: 200 }),
      row('b/y', null, 5, 1),
      row('b/x/deeper', 'ann', 50, 1),
    ]
    expect(foldPrefixes(['gs://b/x/', 'gs://b/y', 'gs://b/gone/'], rows)).toEqual({
      'gs://b/x/': { b: 1000, o: 6, d: 200, a: 9, us: [['ann', 300], ['bob', 100]], cb: { 3: 300, 4: 50 } },
      'gs://b/y': { b: 5, o: 1 },
    })
  })

  it('the same path asked twice (with and without the slash) answers both', () => {
    expect(foldPrefixes(['gs://b/x/', 'gs://b/x'], [row('b/x', null, 1, 1)])).toEqual({
      'gs://b/x/': { b: 1, o: 1 },
      'gs://b/x': { b: 1, o: 1 },
    })
  })
})
