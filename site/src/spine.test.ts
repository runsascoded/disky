import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { graftSpine, levelAsk, levelError, levelTree, nodeAt, spineFailure, spinePaths, spineReady, spineStop } from './spine'
import type { TreeNode } from './types'

const dir = (name: string, bytes: number, children?: TreeNode[]): TreeNode => ({ name, kind: 'dir', bytes, objects: bytes, ...(children ? { children } : {}) })

describe('spinePaths', () => {
  it('the root, then each prefix of the drilled path', () => {
    expect([spinePaths(''), spinePaths('bk'), spinePaths('bk/a/b')]).toEqual([[''], ['', 'bk'], ['', 'bk', 'bk/a', 'bk/a/b']])
  })
})

describe('levelAsk: what each level of a drilled page asks', () => {
  const asks = (n: number, o: { filtered: boolean; fullHeld: boolean; stop?: number | null }) => Array.from({ length: n }, (_, i) => levelAsk(i, n, o))
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
  it('the level the spine stops at, short of the drilled path, asks its full view beside its one level', () => {
    expect(asks(4, { filtered: false, fullHeld: false, stop: 1 })).toEqual([
      { full: false, shallow: true },
      { full: true, shallow: true },
      { full: false, shallow: true },
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

// The scan the simulated pages read: all = bk + zz; bk = a + c; a = b + y; b = p + q; p = p1; zz = k. Nothing else is in it.
const SCAN = dir('all', 150, [
  dir('bk', 100, [dir('a', 60, [dir('b', 40, [dir('p', 30, [dir('p1', 20)]), dir('q', 10)]), dir('y', 15)]), dir('c', 30)]),
  dir('zz', 50, [dir('k', 50)]),
])
/** `/api/subtree`'s answer for `path`: its node to every depth, or (`depth=1`) one level of it; a 404 for a path the scan doesn't hold. */
function answer(path: string, shallow: boolean): TreeNode {
  const t = nodeAt(SCAN, path.split('/').filter(Boolean))
  if (!t) throw new Error('404: path not found')
  return shallow ? { ...t, children: t.children?.map(k => dir(k.name, k.bytes)) } : t
}

// A page's subtree requests as TSQ sends them, wired as `App.tsx` wires them: one observer per level and read,
// enabled as `levelAsk` says with `fullHeld` read back from the level's full observer (`subtreeQs[i].data`), the
// answers grafted (`levelTree`, `graftSpine`, `spineReady`) from the observers' results, and one more observer of
// the full view of the level the spine stops at (`spineStop`), re-optioned once per "render" until nothing changes.
// One `QueryClient` across the pages of a session, so a later page reuses what an earlier one fetched.
function session() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  /** A page at `graftPath`. `hold`: requests that stay in flight until `release`d. `fail`: requests that answer 500. */
  const open = (graftPath: string, filtered = false, hold: string[] = [], fail: string[] = []) => {
    const sent: string[] = []
    const gates = new Map<string, () => void>()
    const paths = spinePaths(graftPath)
    const n = paths.length
    const query = (p: string, shallow: boolean) => ({
      queryKey: ['subtree', p, filtered, ...(shallow ? ['depth1'] : [])],
      queryFn: async (): Promise<TreeNode> => {
        const name = `${shallow ? 'depth=1' : 'full'} ${p || '/'}`
        sent.push(name)
        if (hold.includes(name)) await new Promise<void>(r => gates.set(name, r))
        if (fail.includes(name)) throw new Error('500: boom')
        return answer(p, shallow)
      },
    })
    const fullObs = paths.map(p => new QueryObserver(qc, { ...query(p, false), enabled: false }))
    const shallowObs = paths.map(p => new QueryObserver(qc, { ...query(p, true), enabled: false }))
    const unsubs = [...fullObs, ...shallowObs].map(o => o.subscribe(() => {}))
    let tree: TreeNode | null = null
    let stopObs: { at: number; unsub: () => void } | null = null
    const render = () => {
      paths.forEach((p, i) => {
        const fullHeld = qc.getQueryData(query(p, false).queryKey) !== undefined
        fullObs[i].setOptions({ ...query(p, false), enabled: levelAsk(i, n, { filtered, fullHeld: false }).full })
        shallowObs[i].setOptions({ ...query(p, true), enabled: levelAsk(i, n, { filtered, fullHeld }).shallow })
      })
      const full = fullObs.map(o => o.getCurrentResult()), shallow = shallowObs.map(o => o.getCurrentResult())
      const data = (i: number) => levelTree({ full: full[i].data, shallow: shallow[i].data, deepest: i === n - 1, filtered, next: paths[i + 1]?.split('/').pop() })
      const loading = paths.map((_, i) => !data(i) && (full[i].fetchStatus !== 'idle' || shallow[i].fetchStatus !== 'idle'))
      const grafted = graftSpine(paths, paths.map((_, i) => { const t = data(i); return t && { tree: t, shallow: !full[i].data } }))
      tree = grafted && (filtered || spineReady(grafted, graftPath, loading)) ? grafted : null
      const stop = filtered ? null : spineStop(tree, paths, loading, paths.map((_, i) => !!data(i)))
      if (stopObs && stopObs.at !== stop) { stopObs.unsub(); stopObs = null }
      if (stop != null && !stopObs) stopObs = { at: stop, unsub: new QueryObserver(qc, query(paths[stop], false)).subscribe(() => {}) }
    }
    /** Renders until the page asks nothing more of what has answered. */
    const settle = async () => {
      for (let pass = 0; pass < 6; pass++) {
        render()
        await new Promise(r => setTimeout(r, 0))
      }
      render()
    }
    return {
      sent, settle,
      /** The tree the map draws (null: it holds what it drew before). */
      tree: () => tree,
      release: (name: string) => gates.get(name)!(),
      close: () => [...unsubs, ...(stopObs ? [stopObs.unsub] : [])].forEach(u => u()),
    }
  }
  const visit = async (graftPath: string, filtered = false): Promise<string[]> => {
    const page = open(graftPath, filtered)
    await page.settle()
    page.close()
    return page.sent
  }
  return { open, visit }
}

describe('a drilled page\'s subtree requests', () => {
  it('a cold deep link: one level per ancestor, the full view for the drilled path alone', async () => {
    expect(await session().visit('bk/a/b')).toEqual([
      'depth=1 /',
      'depth=1 bk',
      'depth=1 bk/a',
      'full bk/a/b',
      'depth=1 bk/a/b',
    ])
  })
  it('zooming out asks the full view of the level arrived at, and nothing else', async () => {
    const { visit } = session()
    await visit('bk/a/b')
    expect([await visit('bk/a'), await visit('bk'), await visit('')]).toEqual([['full bk/a'], ['full bk'], ['full /']])
  })
  it('drilling down from the root asks each new level\'s two reads; the ancestors\' held full views are reused', async () => {
    const { visit } = session()
    expect([await visit(''), await visit('bk'), await visit('bk/a'), await visit('bk')]).toEqual([
      ['full /', 'depth=1 /'],
      ['full bk', 'depth=1 bk'],
      ['full bk/a', 'depth=1 bk/a'],
      [],
    ])
  })
  it('a filtered deep link asks every level\'s two reads', async () => {
    expect(await session().visit('bk/a', true)).toEqual(['full /', 'depth=1 /', 'full bk', 'depth=1 bk', 'full bk/a', 'depth=1 bk/a'])
  })
})

describe('a path the scan doesn\'t hold', () => {
  // `bk/a/gone` isn't in the scan: its level and the one below answer 404, and the map draws `bk/a`.
  const COLD = ['depth=1 /', 'depth=1 bk', 'depth=1 bk/a', 'depth=1 bk/a/gone', 'full bk/a/gone/x', 'depth=1 bk/a/gone/x']
  it('the deepest level the scan holds asks its full view once the spine has settled there, and draws it', async () => {
    const page = session().open('bk/a/gone/x')
    await page.settle()
    page.close()
    expect([page.sent, nodeAt(page.tree()!, ['bk', 'a'])]).toEqual([[...COLD, 'full bk/a'], nodeAt(SCAN, ['bk', 'a'])])
  })
  it('a zoom-out to the level it drew asks nothing: its full view is held', async () => {
    const { visit } = session()
    await visit('bk/a/gone/x')
    expect(await visit('bk/a')).toEqual([])
  })
  it('a path missing at the top level draws the root\'s full view', async () => {
    const page = session().open('gone')
    await page.settle()
    page.close()
    expect([page.sent, page.tree()]).toEqual([['depth=1 /', 'full gone', 'depth=1 gone', 'full /'], SCAN])
  })
  it('the full view waits while a level that could still extend the spine is in flight', async () => {
    // `bk/a/gone`'s answer is what says the spine stops at `bk/a`: until it lands, no ancestor is asked its full view.
    const page = session().open('bk/a/gone/x', false, ['depth=1 bk/a/gone'])
    await page.settle()
    const inFlight = [...page.sent]
    page.release('depth=1 bk/a/gone')
    await page.settle()
    page.close()
    expect([inFlight, page.sent]).toEqual([COLD, [...COLD, 'full bk/a']])
  })
})

describe('a level that didn\'t load', () => {
  it('its node on the spine with nothing under it: no full view is asked of it, however long the page stays', async () => {
    // `bk/a`'s one level and both of `bk/a/b`'s reads fail: `bk` drew `a`'s node, bare. The map states the failure in
    // its place (`spineFailure`), and the level that failed is not asked again for a view that has nothing to fill in.
    const COLD = ['depth=1 /', 'depth=1 bk', 'depth=1 bk/a', 'full bk/a/b', 'depth=1 bk/a/b']
    const page = session().open('bk/a/b', false, [], ['depth=1 bk/a', 'full bk/a', 'full bk/a/b', 'depth=1 bk/a/b'])
    await page.settle()
    await page.settle()
    page.close()
    expect([page.sent, nodeAt(page.tree()!, ['bk', 'a'])]).toEqual([COLD, dir('a', 60)])
  })
  it('the spine stopped above it: the level the map draws instead asks its full view', async () => {
    // `zz/hid`'s reads fail and `zz` didn't draw it: the map draws `zz`.
    const page = session().open('zz/hid', false, [], ['full zz/hid', 'depth=1 zz/hid'])
    await page.settle()
    page.close()
    expect([page.sent, nodeAt(page.tree()!, ['zz'])]).toEqual([['depth=1 /', 'depth=1 zz', 'full zz/hid', 'depth=1 zz/hid', 'full zz'], nodeAt(SCAN, ['zz'])])
  })
  it('the fallback\'s own full view failed: asked once, and the map keeps the one level it has', async () => {
    const page = session().open('bk/a/gone', false, [], ['full bk/a'])
    await page.settle()
    await page.settle()
    page.close()
    expect([page.sent, nodeAt(page.tree()!, ['bk', 'a'])]).toEqual([
      ['depth=1 /', 'depth=1 bk', 'depth=1 bk/a', 'full bk/a/gone', 'depth=1 bk/a/gone', 'full bk/a'],
      dir('a', 60, [dir('b', 40), dir('y', 15)]),
    ])
  })
  it('a level a held ancestor drew to depth, whose own read failed: no full view is asked of it', async () => {
    // The viewer drilled down through `bk` (its full view held, drawing `a` and what is under it), then followed a link
    // to a path under `a` the scan doesn't hold, and `a`'s one level fails. The map draws `a` as `bk` drew it, to depth.
    const { open, visit } = session()
    await visit('')
    await visit('bk')
    const page = open('bk/a/gone', false, [], ['depth=1 bk/a', 'full bk/a'])
    await page.settle()
    await page.settle()
    page.close()
    expect([page.sent, nodeAt(page.tree()!, ['bk', 'a'])]).toEqual([['depth=1 bk/a', 'full bk/a/gone', 'depth=1 bk/a/gone'], nodeAt(SCAN, ['bk', 'a'])])
  })
})

describe('a path the scan holds asks no ancestor\'s full view, in whatever order its levels answer', () => {
  const COLD = ['depth=1 /', 'depth=1 bk', 'depth=1 bk/a', 'full bk/a/b', 'depth=1 bk/a/b']
  // While answers are in flight the grafted spine can stop above the drilled path (two levels in a row unanswered:
  // the lower one's own answer has no node to graft under), as it does for good on a path the scan doesn't hold.
  it.each([
    { held: ['depth=1 /'] },
    { held: ['depth=1 bk'] },
    { held: ['depth=1 bk/a'] },
    { held: ['full bk/a/b'] },
    { held: ['depth=1 bk/a/b'] },
    { held: ['depth=1 bk/a/b', 'full bk/a/b'] },
    { held: ['depth=1 bk', 'depth=1 bk/a'] },
    { held: ['depth=1 bk/a', 'depth=1 bk'] },
    { held: ['depth=1 bk/a', 'depth=1 bk/a/b', 'full bk/a/b'] },
    { held: ['depth=1 /', 'depth=1 bk', 'depth=1 bk/a', 'depth=1 bk/a/b', 'full bk/a/b'] },
    { held: ['full bk/a/b', 'depth=1 bk/a/b', 'depth=1 bk/a', 'depth=1 bk', 'depth=1 /'] },
  ])('held, then answering in this order: $held', async ({ held }) => {
    const page = session().open('bk/a/b', false, held)
    await page.settle()
    // What the page has sent while they are in flight, and after each lands.
    const sent = [[...page.sent]]
    for (const name of held) {
      page.release(name)
      await page.settle()
      sent.push([...page.sent])
    }
    page.close()
    expect([sent, nodeAt(page.tree()!, ['bk', 'a', 'b'])]).toEqual([[COLD, ...held.map(() => COLD)], nodeAt(SCAN, ['bk', 'a', 'b'])])
  })
})

describe('graftSpine: the drawn tree from the levels\' answers', () => {
  // The fleet: bk (100) = a (60) + c (30) + 10 folded; a = x (40) + y (15) + 5 folded; x = p (30) + q (10).
  const root1 = dir('all', 150, [dir('bk', 100), dir('zz', 50)])
  const bk1 = dir('bk', 100, [dir('a', 60), dir('c', 30), { ...dir('(other)', 10), folded: 3 }])
  const a1 = dir('a', 60, [dir('x', 40), dir('y', 15), { ...dir('(other)', 5), folded: 2 }])
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
            { ...dir('(other)', 5), folded: 2 },
          ]),
          dir('c', 30),
          { ...dir('(other)', 10), folded: 3 },
        ]),
        dir('zz', 50),
      ]),
    )
  })
  it('a spine segment under its parent\'s fold is taken from its own answer, and the fold shaved by it', () => {
    // `a`'s one level folds `x` away (its budget didn't draw it): `x` comes from its own answer's root.
    const aFolded = dir('a', 60, [dir('y', 15), { ...dir('(other)', 45), folded: 3 }])
    expect(nodeAt(graftSpine(paths, [shallow(root1), shallow(bk1), shallow(aFolded), { tree: xFull, shallow: false }])!, ['bk', 'a'])).toEqual(
      dir('a', 60, [dir('y', 15), { ...dir('(other)', 5), folded: 3 }, xFull]),
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

describe('spineStop: the level a settled spine stops at', () => {
  const tree = dir('all', 150, [dir('bk', 100, [dir('a', 60, [dir('x', 40)]), dir('c', 30)]), dir('zz', 50)])
  const settled = [false, false, false, false, false], all = [true, true, true, true, true]
  const stop = (path: string, loading = settled, answered = all) => spineStop(tree, spinePaths(path), loading, answered)
  it('the deepest level whose node the tree holds, once every level has settled', () => {
    expect([stop('bk/a/gone/p'), stop('bk/gone/x/p'), stop('gone/a/x/p')]).toEqual([2, 1, 0])
  })
  it('none while any level is still loading: its answer may extend the spine', () => {
    expect([
      stop('bk/a/gone/p', [false, false, false, false, true]),
      stop('bk/a/gone/p', [false, false, false, true, false]),
      stop('bk/a/gone/p', [false, true, false, false, false]),
      stop('gone/a/x/p', [true, false, false, false, false]),
    ]).toEqual([null, null, null, null])
  })
  it('none when the level it stops at has no answer of its own: its node is its parent\'s', () => {
    // `bk/a`'s read failed; `bk` drew its node and (a held full view) what is under it.
    expect(stop('bk/a/gone/p', settled, [true, true, false, false, false])).toBe(null)
  })
  it('none when nothing is under the level it stops at: no one level for a full view to fill in', () => {
    // `bk/a/x` is on the spine with no children, whether it answered empty or its read failed.
    expect([stop('bk/a/x/p'), stop('bk/a/x/p', settled, [true, true, true, false, false])]).toEqual([null, null])
  })
  it('none when the tree reaches the drilled path, or there is no tree', () => {
    expect([stop('bk/a/x', settled.slice(1), all.slice(1)), stop('', [false], [true]), spineStop(null, spinePaths('bk/a'), [false, false, false], [true, true, true])]).toEqual([null, null, null])
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
    const bkFolded = dir('bk', 100, [dir('z', 10), { ...dir('(other)', 90), folded: 4 }])
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
