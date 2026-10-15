import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { LINEAGES } from '../_lib/testD1'
import { A, B, C, unionDeployment, Z } from '../_lib/testUnion'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// A union view (specs/federated-scans.md, "First live cases" 1): two stores of one deployment — the primary
// (`v2-search`: roots `bk`, `zz`) and a secondary `meta` (`v2-mount`: root `mb`) — whose roots the main map
// shows side by side, each read at its own scan for the request's cutoff; a drill below the root is the
// owning store's own answer. The deployment: `_lib/testUnion.ts`.

type Node = { n: string; k: string; b: number; o: number; f?: number; c?: Node[]; src?: string; at?: string }
type Body = { date: string; path: string; threshold: number; tree: Node; union?: unknown; nodes: number }

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  let deployment: (views?: Record<string, unknown>) => Env
  const puts: string[] = []

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { puts.push(k.url) } } }
    deployment = await unionDeployment(lineage)
  })

  const get = async (qs: string, views?: Record<string, unknown>) => {
    const r = await subtree({ request: new Request(`http://localhost/api/subtree?w=1280&h=768&${qs}`), env: deployment(views) })
    return { status: r.status, text: await r.text() }
  }
  const body = async (qs: string, views?: Record<string, unknown>) => {
    const r = await get(qs, views)
    expect(r.status).toBe(200)
    return JSON.parse(r.text) as Body
  }
  /** A node's top level as the union lists it. */
  const top = (t: Node) => (t.c ?? []).map(({ n, b, o, src, at }) => ({ n, b, o, ...(src ? { src, at } : {}) }))

  it('no main view: a request without `store=` is the primary alone, exactly as a primary-only deployment answers it', async () => {
    const plain = await get(`date=${A}&path=`, {})
    const named = await get(`date=${A}&path=`, { all: { union: ['primary', 'meta'] } })
    expect([named.status, named.text]).toEqual([200, plain.text])
  })

  it('the main view’s root: each store’s roots at its scan, tagged, under the deployment’s label and the union’s threshold', async () => {
    const [u, p, m] = await Promise.all([body(`date=${A}&path=`), body(`date=${A}&path=&store=primary`), body(`date=${A}&path=&store=meta`)])
    const total = p.tree.b + m.tree.b
    expect({ ...u, tree: { ...u.tree, c: top(u.tree) } }).toEqual({
      date: A, path: '', w: 1280, h: 768, minArea: 12, atten: 2,
      tier: 'bysize', index: 'd1',
      threshold: Math.round(total * 12 / (1280 * 768)),
      nodes: u.nodes,
      truncated: false,
      union: { view: 'all', at: '2026-10-01T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: A }] },
      tree: {
        n: 'Fleet', k: 'dir', b: total, o: p.tree.o + m.tree.o,
        c: [
          ...top(p.tree).filter(c => c.n !== '(other)').map(c => ({ ...c, src: 'primary', at: A })),
          ...top(m.tree).filter(c => c.n !== '(other)').map(c => ({ ...c, src: 'meta', at: A })),
          { n: 'zz', b: 30, o: 2, src: 'primary', at: A },
        ].sort((x, y) => y.b - x.b),
      },
    })
    // `zz` (30 bytes) is under the union's threshold: the treemap's read folds it, but the root lists every
    // store's every root (its bucket table), so it is out of the fold, with no `(other)` left.
    expect(top(u.tree).map(c => [c.n, c.src, c.b])).toEqual([['bk', 'primary', 29339201], ['mb', 'meta', 6291556], ['zz', 'primary', 30]])
  })

  it('`store=<view>` names the view whether or not it is main; `store=primary` is the primary alone', async () => {
    const [named, main, primary] = await Promise.all([
      body(`date=${A}&path=&store=all`, { all: { union: ['primary', 'meta'] } }),
      body(`date=${A}&path=`),
      get(`date=${A}&path=&store=primary`),
    ])
    expect(named).toEqual(main)
    expect(primary.text).toBe((await get(`date=${A}&path=`, {})).text)
  })

  it('a drill below the root is the owning store’s own answer, at that store’s scan', async () => {
    for (const [path, store] of [['mb', 'meta'], ['mb/scans', 'meta'], ['bk', 'primary'], ['zz', 'primary']] as const) {
      const [u, own] = await Promise.all([get(`date=${A}&path=${path}`), get(`date=${A}&path=${path}&store=${store}`)])
      expect([path, u.status, u.text]).toEqual([path, 200, own.text])
    }
  })

  it('each store reads its latest scan through the cutoff: carried forward, or unknown before its first', async () => {
    // C: the primary's scan; meta's latest by then is B.
    const c = await body(`date=${C}&path=`)
    expect([c.union, top(c.tree).map(n => [n.n, n.src, n.at])]).toEqual([
      { view: 'all', at: '2026-10-03T00:01:00.000Z', sources: [{ store: 'primary', date: C }, { store: 'meta', date: B }] },
      [['bk', 'primary', C], ['mb', 'meta', B]],
    ])
    // B: meta's scan; the primary's latest by then is A.
    const b = await body(`date=${B}&path=`)
    expect(b.union).toEqual({ view: 'all', at: '2026-10-02T00:01:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: B }] })
    // A drill takes the owning store's resolved scan: meta's B for `mb` at cutoff C.
    expect((await get(`date=${C}&path=mb`)).text).toBe((await get(`date=${B}&path=mb&store=meta`)).text)
  })

  it('a store with no scan by the cutoff is unknown: absent from the root, named with a null scan', async () => {
    const [u, p] = await Promise.all([body(`date=${Z}&path=`), body(`date=${Z}&path=&store=primary`)])
    expect([u.union, u.tree.b, top(u.tree).map(n => [n.n, n.src, n.at])]).toEqual([
      { view: 'all', at: '2026-09-30T00:01:00.000Z', sources: [{ store: 'primary', date: Z }, { store: 'meta', date: null }] },
      p.tree.b,
      [['bk', 'primary', Z], ['zz', 'primary', Z]],
    ])
    expect(await get(`date=${Z}&path=mb`)).toEqual({ status: 404, text: 'path not found' })
  })

  it('`d=<slug>`: the latest scan any store indexes under it', async () => {
    const [d, exact] = await Promise.all([body('d=261003&path='), body(`date=${C}&path=`)])
    expect(d).toEqual(exact)
    expect(await get('d=261002&path=')).toEqual({ status: 200, text: (await get(`date=${B}&path=`)).text })
  })

  it('`at=<instant>`: each store at its own newest scan at or before it; a store with none unknown', async () => {
    // Between B and C: the primary's newest is A, meta's B. The body's `date`: the newest observation by then.
    const mid = await body('at=20261002T1200Z&path=')
    const b = await body(`date=${B}&path=`)
    expect(mid).toEqual({ ...b, union: { view: 'all', at: '2026-10-02T12:00:00.000Z', sources: [{ store: 'primary', date: A }, { store: 'meta', date: B }] } })
    // Before meta's first scan: the primary's Z alone, meta named unknown; ISO and compact forms alike.
    for (const at of ['2026-09-30T12:00Z', '2026-09-30T12:00:00.000Z', '20260930T120000Z']) {
      const z = await body(`at=${at}&path=`)
      expect([z.date, z.union, top(z.tree).map(n => [n.n, n.src, n.at])]).toEqual([
        Z,
        { view: 'all', at: '2026-09-30T12:00:00.000Z', sources: [{ store: 'primary', date: Z }, { store: 'meta', date: null }] },
        [['bk', 'primary', Z], ['zz', 'primary', Z]],
      ])
    }
    // A drill: the owning store's own answer at its scan for the instant (`at` never reaches the member).
    expect((await get('at=20261003T1200Z&path=mb')).text).toBe((await get(`date=${B}&path=mb&store=meta`)).text)
    // `date=` wins over `at=`; a malformed or too-early instant is refused.
    expect((await get(`date=${A}&at=20261003T1200Z&path=`)).text).toBe((await get(`date=${A}&path=`)).text)
    expect([await get('at=2026-10-02&path='), await get('at=20260929T0000Z&path=')]).toEqual([
      { status: 400, text: '{"error":"at=<UTC instant> (YYYY-MM-DDTHH:MM[:SS]Z or YYYYMMDDTHHMM[SS]Z) required"}\n' },
      { status: 404, text: '{"error":"no scan at or before at=20260929T0000Z"}\n' },
    ])
  })

  it('misses are 404s, never another scan or another store', async () => {
    expect(await get('date=2026-10-04T0001&path=')).toEqual({ status: 404, text: '{"error":"no scan matches date=2026-10-04T0001"}\n' })
    expect(await get('d=2611&path=')).toEqual({ status: 404, text: '{"error":"no scan matches d=2611"}\n' })
    expect(await get(`date=${A}&path=nope/x`)).toEqual({ status: 404, text: 'path not found' })
    // `mb` at B: the primary carries A forward, meta reads B — still meta's.
    expect((await get(`date=${B}&path=mb`)).status).toBe(200)
  })

  it('a root two stores share is a misconfigured union (500), never a sum', async () => {
    const views = { all: { union: ['primary', 'dup'], main: true } }
    expect(await get(`date=${A}&path=`, views)).toEqual({ status: 500, text: "union view 'all': root 'bk' is in stores primary, dup" })
    expect(await get(`date=${A}&path=bk`, views)).toEqual({ status: 500, text: "union view 'all': root 'bk' is in stores primary, dup" })
  })

  it('the root refuses one store’s scope axes; a drill takes them', async () => {
    expect(await get(`date=${A}&path=&o=unowned&cl=s`)).toEqual({ status: 400, text: 'a union view\'s root is unscoped (o, cl: drill into one store)' })
    expect((await get(`date=${A}&path=bk&cl=s`)).text).toBe((await get(`date=${A}&path=bk&cl=s&store=primary`)).text)
  })

  it('each member’s cache key carries its resolved scan, and the union root’s reads the shared threshold', async () => {
    const thr = (await body(`date=${C}&path=`)).tree.b * 12 / (1280 * 768)
    puts.length = 0
    await body(`date=${C}&path=&depth=1`)
    const tail = (label: string) => `?w=1280&h=768&a=12&t=2&l=&o=&b=&D=1&cl=&x=0&F=1&qs=&q=&head=0&g=<g>&st=&rl=${label}&T=${thr}`
    expect(puts.map(k => decodeURIComponent(k).replace(/&g=[^&]+/, '&g=<g>')).sort()).toEqual([
      `https://subtree.cache/v5/~localhost/${C}/${tail('Fleet')}`,
      `https://subtree.cache/v5/~localhost/@meta/${B}/${tail('our storage')}`,
    ])
  })
})
