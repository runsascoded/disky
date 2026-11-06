import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { graftSpine, levelAsk, levelError, levelTree, nodeAt, spineFailure, spinePaths, spineReady } from './spine'
import type { TreeNode } from './types'

const dir = (n: string, b: number, c?: TreeNode[]): TreeNode => ({ n, k: 'dir', b, o: b, ...(c ? { c } : {}) })

describe('spinePaths', () => {
  it('the root, then each prefix of the drilled path', () => {
    expect([spinePaths(''), spinePaths('bk'), spinePaths('bk/a/b')]).toEqual([[''], ['', 'bk'], ['', 'bk', 'bk/a', 'bk/a/b']])
  })
})

describe('levelAsk: what each level of a drilled page asks', () => {
  const asks = (n: number, o: { filtered: boolean; fullHeld: boolean }) => Array.from({ length: n }, (_, i) => levelAsk(i, n, o))
  it('unfiltered: only the deepest level asks its full view; ancestors ask one level', () => {
    expect(asks(4, { filtered: false, fullHeld: false })).toEqual([
      { full: false, shallow: true },
      { full: false, shallow: true },
      { full: false, shallow: true },
      { full: true, shallow: true },
    ])
  })
  it('an ancestor whose full view is already held asks nothing', () => {
    expect(asks(3, { filtered: false, fullHeld: true })).toEqual([
      { full: false, shallow: false },
      { full: false, shallow: false },
      { full: true, shallow: true },
    ])
  })
  it('the store root alone asks both', () => {
    expect(asks(1, { filtered: false, fullHeld: false })).toEqual([{ full: true, shallow: true }])
  })
  it('under a name filter every level asks both, held or not', () => {
    const both = { full: true, shallow: true }
    expect([asks(3, { filtered: true, fullHeld: false }), asks(3, { filtered: true, fullHeld: true })]).toEqual([[both, both, both], [both, both, both]])
  })
})

describe('levelTree: the tree a level gives the graft', () => {
  const full = dir('a', 10, [dir('x', 6, [dir('y', 5)])])
  const shallow = dir('a', 10, [dir('x', 6)])
  it('unfiltered: the full view once held, else the shallow one, at any level', () => {
    expect([
      levelTree({ full, shallow, deepest: false, filtered: false, next: 'x' }),
      levelTree({ shallow, deepest: false, filtered: false, next: 'x' }),
      levelTree({ shallow, deepest: true, filtered: false }),
      levelTree({ deepest: false, filtered: false, next: 'x' }),
    ]).toEqual([full, shallow, shallow, null])
  })
  it('filtered: an ancestor\'s first-paint forest stands in only when it holds the next segment with children', () => {
    expect([
      levelTree({ shallow: full, deepest: false, filtered: true, next: 'x' }),
      levelTree({ shallow, deepest: false, filtered: true, next: 'x' }),
      levelTree({ shallow, deepest: true, filtered: true }),
    ]).toEqual([full, null, shallow])
  })
})

// A page's subtree requests as TSQ sends them: one observer per level and read, enabled as `levelAsk` says with
// `fullHeld` read back from the level's full observer (as `App.tsx` reads `subtreeQs[i].data`), re-optioned once
// per "render" until nothing changes. One `QueryClient` across the visits of a session, so a later page reuses
// what an earlier one fetched.
function session() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const visit = async (graftPath: string, filtered = false): Promise<string[]> => {
    const sent: string[] = []
    const paths = spinePaths(graftPath)
    const n = paths.length
    const fn = (url: string) => async () => { sent.push(url); return url }
    const opts = (held: boolean[]) => paths.flatMap((p, i) => {
      const ask = levelAsk(i, n, { filtered, fullHeld: held[i] })
      return [
        { queryKey: ['subtree', p, filtered], enabled: ask.full, queryFn: fn(`full ${p || '/'}`) },
        { queryKey: ['subtree', p, filtered, 'depth1'], enabled: ask.shallow, queryFn: fn(`depth=1 ${p || '/'}`) },
      ]
    })
    const held = () => paths.map(p => qc.getQueryData(['subtree', p, filtered]) !== undefined)
    const observers = opts(held()).map(o => new QueryObserver(qc, o))
    const unsubs = observers.map(o => o.subscribe(() => {}))
    for (let pass = 0; pass < 3; pass++) {
      await new Promise(r => setTimeout(r, 0))
      opts(held()).forEach((o, i) => observers[i].setOptions(o))
    }
    unsubs.forEach(u => u())
    return sent
  }
  return visit
}

describe('a drilled page\'s subtree requests', () => {
  it('a cold deep link: one level per ancestor, the full view for the drilled path alone', async () => {
    expect(await session()('bk/a/b')).toEqual([
      'depth=1 /',
      'depth=1 bk',
      'depth=1 bk/a',
      'full bk/a/b',
      'depth=1 bk/a/b',
    ])
  })
  it('zooming out asks the full view of the level arrived at, and nothing else', async () => {
    const visit = session()
    await visit('bk/a/b')
    expect([await visit('bk/a'), await visit('bk'), await visit('')]).toEqual([['full bk/a'], ['full bk'], ['full /']])
  })
  it('drilling down from the root asks each new level\'s two reads; the ancestors\' held full views are reused', async () => {
    const visit = session()
    expect([await visit(''), await visit('bk'), await visit('bk/a'), await visit('bk')]).toEqual([
      ['full /', 'depth=1 /'],
      ['full bk', 'depth=1 bk'],
      ['full bk/a', 'depth=1 bk/a'],
      [],
    ])
  })
  it('a filtered deep link asks every level\'s two reads', async () => {
    expect(await session()('bk/a', true)).toEqual(['full /', 'depth=1 /', 'full bk', 'depth=1 bk', 'full bk/a', 'depth=1 bk/a'])
  })
})

describe('graftSpine: the drawn tree from the levels\' answers', () => {
  // The fleet: bk (100) = a (60) + c (30) + 10 folded; a = x (40) + y (15) + 5 folded; x = p (30) + q (10).
  const root1 = dir('all', 150, [dir('bk', 100), dir('zz', 50)])
  const bk1 = dir('bk', 100, [dir('a', 60), dir('c', 30), { ...dir('(other)', 10), f: 3 }])
  const a1 = dir('a', 60, [dir('x', 40), dir('y', 15), { ...dir('(other)', 5), f: 2 }])
  const xFull = dir('x', 40, [dir('p', 30, [dir('p1', 20), dir('p2', 10)]), dir('q', 10)])
  const paths = spinePaths('bk/a/x')
  const shallow = (tree: TreeNode) => ({ tree, shallow: true })

  it('shallow ancestors and the drilled path\'s full view: each spine node keeps its parent\'s totals and takes its own children', () => {
    expect(graftSpine(paths, [shallow(root1), shallow(bk1), shallow(a1), { tree: xFull, shallow: false }])).toEqual(
      dir('all', 150, [
        dir('bk', 100, [
          dir('a', 60, [
            dir('x', 40, [dir('p', 30, [dir('p1', 20), dir('p2', 10)]), dir('q', 10)]),
            dir('y', 15),
            { ...dir('(other)', 5), f: 2 },
          ]),
          dir('c', 30),
          { ...dir('(other)', 10), f: 3 },
        ]),
        dir('zz', 50),
      ]),
    )
  })
  it('a spine segment under its parent\'s fold is taken from its own answer, and the fold shaved by it', () => {
    // `a`'s one level folds `x` away (its budget didn't draw it): `x` comes from its own answer's root.
    const aFolded = dir('a', 60, [dir('y', 15), { ...dir('(other)', 45), f: 3 }])
    expect(nodeAt(graftSpine(paths, [shallow(root1), shallow(bk1), shallow(aFolded), { tree: xFull, shallow: false }])!, ['bk', 'a'])).toEqual(
      dir('a', 60, [dir('y', 15), { ...dir('(other)', 5), f: 3 }, xFull]),
    )
  })
  it('a held full ancestor keeps the depth it drew under the next level\'s shallow answer', () => {
    // The viewer drilled down through `bk` (its full view held, drawing `a` to depth): `a`'s one level doesn't flatten it.
    const bkFull = dir('bk', 100, [dir('a', 60, [dir('x', 40, [dir('p', 30)]), dir('y', 15)]), dir('c', 30)])
    expect(nodeAt(graftSpine(spinePaths('bk/a'), [shallow(root1), { tree: bkFull, shallow: false }, shallow(a1)])!, ['bk', 'a'])).toEqual(
      dir('a', 60, [dir('x', 40, [dir('p', 30)]), dir('y', 15)]),
    )
  })
  it('is null until the root level answers', () => {
    expect(graftSpine(paths, [null, shallow(bk1), shallow(a1), { tree: xFull, shallow: false }])).toBe(null)
  })
})

describe('spineReady: when the grafted tree may be drawn', () => {
  const tree = dir('all', 150, [dir('bk', 100, [dir('a', 60, [dir('x', 40)]), dir('c', 30)]), dir('zz', 50)])
  const ready = (path: string, loading: boolean[]) => spineReady(tree, path, loading)
  it('held back while the spine stops short and a level is still loading', () => {
    expect([
      ready('bk/a/x/p', [false, false, false, false, true]),
      ready('bk/a/x/p', [false, false, false, true, false]),
    ]).toEqual([false, false])
  })
  it('drawn truncated once every level has settled (a path the scan doesn\'t hold)', () => {
    expect(ready('bk/a/x/p', [false, false, false, false, false])).toBe(true)
  })
  it('held back while the drilled node has no children and its own level is loading', () => {
    expect([ready('bk/a/x', [false, false, false, true]), ready('bk/a/x', [false, false, false, false])]).toEqual([false, true])
  })
  it('drawn as soon as the drilled node has children, whatever else loads', () => {
    expect([ready('bk/a', [true, true, true]), ready('', [true])]).toEqual([true, true])
  })
})

describe('levelError: a level\'s failure, once it has settled with no answer', () => {
  const full = new Error('500: full'), shallow = new Error('500: depth=1')
  it('none while the level has an answer or a read still in flight (its retries included)', () => {
    expect([
      levelError({ answered: true, loading: false, full, shallow }),
      levelError({ answered: false, loading: true, shallow }),
      levelError({ answered: false, loading: false }),
    ]).toEqual([undefined, undefined, undefined])
  })
  it('an ancestor\'s is its one level\'s; the drilled level\'s is its full view\'s, the read the map waits on', () => {
    expect([
      levelError({ answered: false, loading: false, shallow }),
      levelError({ answered: false, loading: false, full, shallow }),
    ]).toEqual([shallow, full])
  })
})

describe('spineFailure: the failed level that keeps the map off the drilled path', () => {
  // bk = a (90) + z (10); a = b (80); b = c (70); c = x (60). Every level answers one level deep, as on a cold deep link.
  const paths = spinePaths('bk/a/b/c')
  const root = dir('all', 100, [dir('bk', 100)])
  const bk = dir('bk', 100, [dir('a', 90), dir('z', 10)])
  const a = dir('a', 90, [dir('b', 80)])
  const b = dir('b', 80, [dir('c', 70)])
  const c = dir('c', 70, [dir('x', 60)])
  const E500 = new Error('500: boom'), E404 = new Error('404: path not found')
  /** The grafted tree of `levels` (null: no answer), the path it reaches, and the failure reported for `errs`. */
  const drawn = (levels: (TreeNode | null)[], errs: (Error | undefined)[]) => {
    const tree = graftSpine(paths, levels.map(t => t && { tree: t, shallow: true }))!
    const reached = ['bk', 'a', 'b', 'c'].filter((_, i, segs) => nodeAt(tree, segs.slice(0, i + 1))).join('/')
    return [reached, spineFailure(tree, paths, errs)]
  }
  const none = [undefined, undefined, undefined, undefined, undefined]

  it('every level answered: the path is drawn, nothing failed', () => {
    expect(drawn([root, bk, a, b, c], none)).toEqual(['bk/a/b/c', null])
  })
  it('one failed ancestor whose node its parent drew: the level below grafts itself under that node, and the path is still drawn', () => {
    expect(drawn([root, bk, null, b, c], [undefined, undefined, E500, undefined, undefined])).toEqual(['bk/a/b/c', null])
  })
  it('two failed levels in a row: the spine stops at the first one\'s node, bare, and that level is the one reported', () => {
    expect(drawn([root, bk, null, null, c], [undefined, undefined, E500, E500, undefined])).toEqual(['bk/a', { path: 'bk/a', err: E500, bare: true }])
  })
  it('a failed ancestor its parent folded into "(other)": the spine stops above it', () => {
    const bkFolded = dir('bk', 100, [dir('z', 10), { ...dir('(other)', 90), f: 4 }])
    expect(drawn([root, bkFolded, null, b, c], [undefined, undefined, E500, undefined, undefined])).toEqual(['bk', { path: 'bk/a', err: E500, bare: false }])
  })
  it('the drilled level failed: its node is on the spine with nothing under it (bare)', () => {
    expect(drawn([root, bk, a, b, null], [undefined, undefined, undefined, undefined, E500])).toEqual(['bk/a/b/c', { path: 'bk/a/b/c', err: E500, bare: true }])
  })
  it('a path the scan doesn\'t hold: every level from the first missing one is refused, and the first is the one reported', () => {
    const aWithout = dir('a', 90, [dir('y', 80)])
    expect(drawn([root, bk, aWithout, null, null], [undefined, undefined, undefined, E404, E404])).toEqual(['bk/a', { path: 'bk/a/b', err: E404, bare: false }])
  })
  it('a failed ancestor the spine got past is not reported for a level further down', () => {
    // `bk/a` failed (grafted past, as above); `bk/a/b/c` is what the spine lacks.
    expect(drawn([root, bk, null, dir('b', 80, [dir('y', 70)]), null], [undefined, undefined, E500, undefined, E404])).toEqual(['bk/a/b', { path: 'bk/a/b/c', err: E404, bare: false }])
  })
  it('a short spine with no failure (levels still loading, or settled without one) reports none', () => {
    expect(drawn([root, bk, null, null, null], none)).toEqual(['bk/a', null])
  })
  it('a drilled level that answered empty is drawn as it is, whatever failed above it', () => {
    expect(drawn([root, bk, null, b, dir('c', 70)], [undefined, undefined, E500, undefined, undefined])).toEqual(['bk/a/b/c', null])
  })
})
