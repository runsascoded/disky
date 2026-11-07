/** Brute force for the map's filter, straight from the static-filter fixture's objects (`fixtures/static-filter/gen.py`
 *  `BASE` / `scan_objects`, mirrored here): what a filtered view must show, for any query the predicate
 *  (`pathQuery.ts` `compileQuery`) expresses — and a `HitSource` answering any key by brute force (`objectsSource`),
 *  for keys the fixture's index doesn't hold (anchored ones). Test-only. */
import type { NamePred } from './pathQuery'
import type { Found, HitSource } from './staticFilter'
import { termInPath } from './staticAnchors'
import { type Hit, scanAt } from './staticNames'

/** A scan's objects: `bucket → [key, size, owner | null][]`. */
export type Objects = Record<string, [string, number, string | null][]>

const MiB = 1 << 20
export const SCANS = ['2026-10-04', '2026-10-05']

/** `gen.py`'s `BASE`. */
function baseObjects(): Objects {
  const fill: [string, number, null][] = Array.from({ length: 3000 }, (_, i) => [`fill/f${String(i).padStart(5, '0')}`, 1 << (i % 12), null])
  return {
    bk: [...fill,
      ['data/tomato/a.bin', 5000, 'alice'], ['data/tomato/b.bin', 3000, 'bob'],
      ['data/Tomatoes.csv', 700, null],
      ['data/raw/tomat-1/x.bin', 100, null], ['data/raw/tomat-1/TOMAT-inner/y.bin', 50, 'alice'],
      ['data/raw/plain.bin', 33, null],
      ['runs/x/ckpt-tomat.pt', 9000, 'alice'], ['runs/x/ckpt-2.pt', 1200, 'alice'], ['runs/x/tomat-tomat.bin', 77, null],
      ['models/llama/model.safetensors', 8 * MiB, 'bob'], ['models/tiny.safetensors', 10, null],
      ['tmp/ttl=7d/z.bin', 2 * MiB, null], ['tmp/ttl=14d/q.bin', MiB, 'bob'],
    ],
    'tomato-bk': [['a/b.bin', 100, null], ['c/tomat.txt', 10, 'carol']],
    zz: [['Checkpoints/TOMAT/a.bin', 10, null], ['data/x.parquet', 20, null]],
  }
}

/** `gen.py`'s `scan_objects`. */
export function fixtureObjects(date: string): Objects {
  const out = baseObjects()
  if (date === SCANS[1]) {
    out.bk = out.bk.filter(o => o[0] !== 'data/Tomatoes.csv' && o[0] !== 'tmp/ttl=7d/z.bin')
      .map(([k, s, u]) => [k, k === 'runs/x/ckpt-tomat.pt' ? 9500 : s, u] as [string, number, string | null])
    out.bk.push(['data/tomato/c.bin', 2000, 'alice'], ['new-tomat/n.bin', 400, null])
    out.zz = out.zz.map(([k, s, u]) => [k, k === 'data/x.parquet' ? 25 : s, u])
  }
  return out
}

/** The view's answer: its match roots (outermost matching paths under `root`; `[root]` when the query
 *  matches it) with their bytes and objects net of their excluded descendants, the excluded paths (outermost
 *  descendants of a root the negative part holds), and the matched total. `owner` keeps only those
 *  objects (null = unowned). */
export interface Brute {
  roots: [string, number, number][]
  excluded: string[]
  total: [number, number]
}

const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i === -1 ? '' : p.slice(0, i) }

export function brute(objects: Objects, pred: NamePred, root: string, owner?: (usr: string | null) => boolean): Brute {
  const roots = new Map<string, [number, number]>()
  const excluded = new Set<string>()
  const rootHit = pred(root)
  for (const [bucket, objs] of Object.entries(objects)) {
    for (const [key, size, usr] of objs) {
      const full = `${bucket}/${key}`
      if (root && !full.startsWith(root + '/')) continue
      // The path's ancestors under `root`, outermost first, then itself.
      const segs = full.split('/')
      const start = root ? root.split('/').length : 0
      const chain = segs.map((_, i) => segs.slice(0, i + 1).join('/')).slice(start)
      let r: string | null = rootHit ? root : null
      if (r === null) for (const p of chain) if (pred(p)) { r = p; break }
      if (r === null) continue
      // Excluded: the outermost path at or under the root that the negative part holds (the positive part
      // holds every path under a root: it is monotone).
      let ex: string | null = null
      if (pred.neg) for (const p of chain) if ((r === root || p === r || p.startsWith(r + '/')) && pred.neg(p)) { ex = p; break }
      if (ex !== null) { excluded.add(ex); continue }
      const t = roots.get(r) ?? [0, 0]
      if (!owner || owner(usr)) { t[0] += size; t[1] += 1 }
      roots.set(r, t)
    }
  }
  // An excluded path is outermost already (the first in its chain); a root whose every object is excluded
  // still has its net (zero) row.
  for (const e of excluded) {
    let r = rootHit ? root : null
    for (let p = parentOf(e); !rootHit && p.length > root.length; p = parentOf(p)) { if (pred(p)) r = p; if (p === '') break }
    if (r !== null && !roots.has(r)) roots.set(r, [0, 0])
  }
  const rows = [...roots].map(([p, [b, o]]) => [p, b, o] as [string, number, number]).sort((x, y) => (x[0] < y[0] ? -1 : 1))
  return { roots: rows, excluded: [...excluded].sort(), total: rows.reduce<[number, number]>((t, r) => [t[0] + r[1], t[1] + r[2]], [0, 0]) }
}

/** Any key's first hits (`termKey`: plain or anchored) by brute force over each scan's objects: per (path, owner,
 *  scan) one hit live on that scan alone. */
export function objectsSource(byScan: Record<string, Objects> = Object.fromEntries(SCANS.map(d => [d, fixtureObjects(d)]))): HitSource {
  return {
    async hits(key: string, under: string): Promise<Found | null> {
      if (termInPath(key, under)) return null
      const acc = new Map<string, Hit>()
      for (const [date, objects] of Object.entries(byScan)) {
        const D = scanAt(date)
        for (const [bucket, objs] of Object.entries(objects)) for (const [k, size, usr] of objs) {
          const full = `${bucket}/${k}`
          if (under && !full.startsWith(under + '/')) continue
          const segs = full.split('/')
          for (let i = under ? under.split('/').length : 0; i < segs.length; i++) {
            const p = segs.slice(0, i + 1).join('/')
            if (!termInPath(key, p)) continue
            const id = `${p}\0${usr ?? ''}\0${date}`
            const h = acc.get(id) ?? { path: p, depth: i + 1, usr: usr ?? '', vf: D, vt: D + 1, size: 0n, n: 0n }
            h.size += BigInt(size); h.n += 1n
            acc.set(id, h)
            break
          }
        }
      }
      return { hits: [...acc.values()], io: { from: 'brute' } }
    },
  }
}
