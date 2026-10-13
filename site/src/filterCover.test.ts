import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { COVER_V } from '../functions/_lib/cover'
import { assignInBatches, chainOf, CoverError, coverQuery, coverRetry, fetchCover, type FilterCover, prefetchCover, rowCover, rowItems, matchedListOf, rowLabel, rowMatchesOf, rowsCover, stageInBatches } from './filterCover'
import { stageMany } from './plans'

describe('large sets go in batches, never refused', () => {
  const prefixes = Array.from({ length: 1234 }, (_, i) => `gs://b/d/${i}/`)
  it('assign: 500 actions per POST, in order, with progress', async () => {
    const posts: number[] = [], seen: string[] = [], prog: [number, number][] = []
    const n = await assignInBatches(prefixes, pattern => ({ pattern, owner: '@me' }), async a => { posts.push(a.length); seen.push(...a.map(x => x.pattern)) }, (d, t) => prog.push([d, t]))
    expect([n, posts, seen, prog]).toEqual([1234, [500, 500, 234], prefixes, [[500, 1234], [1000, 1234], [1234, 1234]]])
  })
  it('stage: 500 prefixes per POST, then the batches fold into the first', async () => {
    const calls: unknown[] = []
    let next = 40
    const r = await stageInBatches(prefixes,
      async part => { calls.push(['stage', part.length]); return { plan_id: 7, batch_id: next++ } },
      async (plan, into, ids) => { calls.push(['merge', plan, into, ids]) })
    expect([r, calls]).toEqual([{ plan_id: 7, batch_id: 40, batches: 3 }, [['stage', 500], ['stage', 500], ['stage', 234], ['merge', 7, 40, [41, 42]]]])
  })
  it('stage: one batch is one POST and no merge; a part that staged nothing (all covered) is no batch', async () => {
    const calls: unknown[] = []
    expect(await stageInBatches(prefixes.slice(0, 3), async part => { calls.push(part.length); return { plan_id: 7, batch_id: 9 } }, async () => { calls.push('merge') })).toEqual({ plan_id: 7, batch_id: 9, batches: 1 })
    let i = 0
    expect(await stageInBatches(prefixes.slice(0, 1000), async () => ({ plan_id: 7, batch_id: i++ ? null : 5 }), async () => { calls.push('merge') })).toEqual({ plan_id: 7, batch_id: 5, batches: 1 })
    expect(calls).toEqual([3])
  })
  it('the stage API client sends exactly those requests and returns one result', async () => {
    const sent: [string, string, unknown][] = []
    let b = 1
    const post = (async (url: string, method: string, body: { prefixes?: string[] }) => {
      sent.push([url, method, body.prefixes ? body.prefixes.length : body])
      return url.endsWith('/stage') ? { plan_id: 3, batch_id: b++, staged: body.prefixes, covered: [], absorbed: [], as_of: null } : {}
    }) as never
    const r = await stageMany(prefixes.slice(0, 600), ' why ', post)
    expect([sent, r.plan_id, r.batch_id, r.staged.length]).toEqual([[
      ['/api/plans/stage', 'POST', 500], ['/api/plans/stage', 'POST', 100], ['/api/plans/3/batches/merge', 'POST', { into: 1, ids: [2] }],
    ], 3, 1, 600])
  })
})

describe('a filtered row: its label and its matches', () => {
  it('a row holding one chain of single children shows the path down it, as its tile does', () => {
    expect(chainOf({ n: 'marin-eu-west4', c: [{ n: 'tomat', c: [{ n: 'a' }, { n: 'b' }] }] })).toEqual({ label: 'marin-eu-west4/tomat', segs: ['marin-eu-west4', 'tomat'] })
    expect(chainOf({ n: 'a', c: [{ n: 'b', c: [{ n: 'c', c: [{ n: 'd' }] }] }] })).toEqual({ label: 'a/…/d', segs: ['a', 'b', 'c', 'd'] })
    expect(chainOf({ n: 'a', c: [{ n: '(other)' }] })).toEqual({ label: 'a', segs: ['a'] })
    expect(chainOf({ n: 'a', c: [{ n: 'x' }, { n: 'y' }] })).toEqual({ label: 'a', segs: ['a'] })
  })
  // gcs prod, `?f=tomat`: `marin-us-east5` drew one child (`tomat`), but held 9 match roots — `tomat/` and 8
  // `flan_*_rotten_tomatoes_*.parquet` files too small to draw. Its label named only `tomat`.
  const east5 = { n: 'marin-us-east5', c: [{ n: 'tomat', c: [{ n: 'a' }, { n: 'b' }] }] }
  const flan = Array.from({ length: 8 }, (_, i) => ({ path: `marin-us-east5/data/hrm_text_split/flan_direct/flan_${i}_rotten_tomatoes_part_00000.parquet` }))
  it('a row names the path to its match only when it holds exactly one (cut at the match); several: its name and how many; unknown: its name', () => {
    expect([
      rowLabel(east5, 'marin-us-east5', { n: 1, one: 'marin-us-east5/tomat' }),
      rowLabel(east5, 'marin-us-east5', { n: 9 }),
      rowLabel(east5, 'marin-us-east5', null),
      rowLabel(east5, 'marin-us-east5', { n: 0 }),
      // A capped list's lower bound: several listed → `9+`; one listed says nothing.
      rowLabel(east5, 'marin-us-east5', { n: 9, atLeast: true }),
      rowLabel(east5, 'marin-us-east5', { n: 1, atLeast: true }),
      // The drawn chain runs past the match (`tomat` drew one child): the label stops at the match.
      rowLabel({ n: 'b', c: [{ n: 'tomat', c: [{ n: 'only' }] }] }, 'b', { n: 1, one: 'b/tomat' }),
      // The row is the match itself.
      rowLabel(east5, 'marin-us-east5', { n: 1, one: 'marin-us-east5' }),
      // One match the drawn chain doesn't reach (drawn elsewhere, or not at all): the name and the count.
      rowLabel(east5, 'marin-us-east5', { n: 1, one: flan[0].path }),
      rowLabel({ n: 'a', c: [{ n: 'b', c: [{ n: 'c', c: [{ n: 'd' }] }] }] }, 'x/a', { n: 1, one: 'x/a/b/c/d' }),
    ]).toEqual([
      { label: 'marin-us-east5/tomat', segs: ['marin-us-east5', 'tomat'] },
      { label: 'marin-us-east5', segs: ['marin-us-east5'], count: 9 },
      { label: 'marin-us-east5', segs: ['marin-us-east5'] },
      { label: 'marin-us-east5', segs: ['marin-us-east5'] },
      { label: 'marin-us-east5', segs: ['marin-us-east5'], count: 9, atLeast: true },
      { label: 'marin-us-east5', segs: ['marin-us-east5'] },
      { label: 'b/tomat', segs: ['b', 'tomat'] },
      { label: 'marin-us-east5', segs: ['marin-us-east5'] },
      { label: 'marin-us-east5', segs: ['marin-us-east5'], count: 1 },
      { label: 'a/…/d', segs: ['a', 'b', 'c', 'd'] },
    ])
  })
  it('a row\'s match count: exact from a full root list, a lower bound (more than one listed) from a capped one, else unknown', () => {
    const matched = [{ path: 'marin-us-east5/tomat' }, ...flan, { path: 'marin-us-east5x/tomat' }, { path: 'marin-eu-west4/tomat' }]
    const exact = rowMatchesOf(matched, 'exact')
    const capped = rowMatchesOf(matched, 'capped')
    expect([
      exact('marin-us-east5'), exact('marin-eu-west4'), exact('marin-us-east5/tomat'), exact('bkt-none'),
      capped('marin-us-east5'), capped('marin-eu-west4'), capped('bkt-none'),
      rowMatchesOf(matched, null)('marin-us-east5'), rowMatchesOf(undefined, 'exact')('marin-us-east5'),
    ]).toEqual([
      { n: 9 }, { n: 1, one: 'marin-eu-west4/tomat' }, { n: 1, one: 'marin-us-east5/tomat' }, { n: 0 },
      { n: 9, atLeast: true }, null, null,
      null, null,
    ])
  })
  it('a response\'s list is exact only uncapped and complete: capped, or unknown when partial, approximate, a rollup or a dir-only scan', () => {
    const m = [{ path: 'a/tomat', b: 1, o: 1 }]
    expect([
      matchedListOf({ matched: m }),
      matchedListOf({ matched: m, matchesCapped: true }),
      matchedListOf({ matched: m, partialReason: 'cut' }),
      matchedListOf({ matched: m, approximateReason: 'no index' }),
      matchedListOf({ matched: m, rollup: {} }),
      matchedListOf({ matched: m, dirsOnly: true }),
      matchedListOf({}),
      matchedListOf(undefined),
    ]).toEqual(['exact', 'capped', null, null, null, null, null, null])
  })
  // gcs prod, 2026-10-09 scan, `/api/subtree?q=tomat&full=1` (shape as served; names shortened): the fleet
  // root lists 205 of 21,735 match roots (`matchesCapped`), the paths below the store root as the rows' are;
  // drilled into `marin-us-east5`, the list is every root (9).
  it('the real response shapes: the capped root says `9+`, never the path; the drilled exact list names its one match', () => {
    const P = 'marin-us-east5/data/hrm_text_split/flan_direct/'
    const e5 = ['marin-us-east5/tomat', ...['t0_fsopt', 'flan_fsopt', 'flan_fsnoopt', 't0_fsnoopt', 't0_zsopt', 't0_zsnoopt', 'flan_zsopt', 'flan_zsnoopt'].map(f => `${P}${f}_part_00000.parquet`)]
    const root = {
      matched: [
        { path: 'marin-eu-west4/tomat', b: 4543930137403, o: 420738 }, { path: e5[0], b: 2120947096049, o: 121162 },
        { path: 'marin-us-central1/tomat', b: 486396327578, o: 19893 }, { path: 'marin-us-east1/tomat', b: 212692920492, o: 18791 },
        ...Array.from({ length: 193 }, (_, i) => ({ path: `marin-us-central1/podcast_audio/show/ep${i}_rotten_tomatoes.mp3`, b: 1000 - i, o: 1 })),
        ...e5.slice(1).map((path, i) => ({ path, b: 100 - i, o: 1 })),
      ],
      matchCount: { n: 21735, b: 7396315609520, o: 603889 }, matchesCapped: true,
    }
    const drilled = { matched: e5.map((path, i) => ({ path, b: 9 - i, o: 1 })), matchCount: { n: 9, b: 2121011255780, o: 121170 } }
    const atRoot = rowMatchesOf(root.matched, matchedListOf(root))
    const inE5 = rowMatchesOf(drilled.matched, matchedListOf(drilled))
    const e5Node = { n: 'marin-us-east5', c: [{ n: 'tomat', c: [{ n: 'results' }, { n: 'cache' }, { n: 'x' }] }] }
    expect([
      root.matched.length,
      rowLabel(e5Node, 'marin-us-east5', atRoot('marin-us-east5')),
      rowLabel({ n: 'marin-eu-west4', c: [{ n: 'tomat' }] }, 'marin-eu-west4', atRoot('marin-eu-west4')),
      rowLabel({ n: 'tomat', c: [{ n: 'results' }, { n: 'cache' }] }, 'marin-us-east5/tomat', inE5('marin-us-east5/tomat')),
      rowLabel({ n: 'data', c: [{ n: 'hrm_text_split', c: [{ n: 'flan_direct' }] }] }, 'marin-us-east5/data', inE5('marin-us-east5/data')),
    ]).toEqual([
      205,
      { label: 'marin-us-east5', segs: ['marin-us-east5'], count: 9, atLeast: true },
      { label: 'marin-eu-west4', segs: ['marin-eu-west4'] },
      { label: 'tomat', segs: ['tomat'] },
      { label: 'data', segs: ['data'], count: 8 },
    ])
  })
  it('a row\'s matches are the items at or under it, never a sibling sharing its name as a prefix', () => {
    const items = [{ path: 'b/x/tomat', kind: 'dir' as const, b: 1, o: 1, roots: 1 }, { path: 'b/xy/tomat', kind: 'dir' as const, b: 2, o: 1, roots: 1 }, { path: 'b/x', kind: 'dir' as const, b: 3, o: 1, roots: 1 }]
    expect(rowItems(items, 'b/x').map(i => i.path)).toEqual(['b/x/tomat', 'b/x'])
  })
})

describe('the cover is fetched on demand, and retried once on a 5xx', () => {
  const COVER = { date: 'D', path: 'p', q: 'nemotron', items: [], roots: { n: 0, b: 0, o: 0 }, complete: true, looked: 0, unchecked: 0 }
  /** A fetcher answering `statuses` in turn (then 200 with `COVER`), recording each URL. */
  const fetcher = (statuses: number[]) => {
    const urls: string[] = []
    const f = async (url: string) => {
      urls.push(url)
      const s = statuses.shift() ?? 200
      return new Response(s === 200 ? JSON.stringify(COVER) : JSON.stringify({ error: `boom ${s}` }), { status: s })
    }
    return { f, urls }
  }
  const args = { date: 'D', path: 'p', q: 'nemotron', qs: 'simple' }
  const URL = `/api/filter-cover?cv=${COVER_V}&date=D&path=p&q=nemotron&qs=simple`
  /** The query as the page observes it (`useQuery`'s observer), settled; retries without their delay. */
  const observe = async (f: (u: string) => Promise<Response>, enabled: boolean) => {
    const client = new QueryClient()
    const obs = new QueryObserver(client, { ...coverQuery(f, 'primary', { ...args, enabled }), retryDelay: 0 })
    const unsub = obs.subscribe(() => {})
    await new Promise(r => setTimeout(r, 20))
    const r = obs.getCurrentResult()
    unsub()
    return [r.status, r.fetchStatus, r.failureCount, r.error ? [r.error.constructor.name, r.error.message] : null, r.data ?? null]
  }

  it('not wanted: no request at all', async () => {
    const { f, urls } = fetcher([])
    expect([await observe(f, false), urls]).toEqual([['pending', 'idle', 0, null, null], []])
  })

  it('wanted: one request; a 503 then a 200 is two, and the answer', async () => {
    const ok = fetcher([])
    const flaky = fetcher([503])
    expect([await observe(ok.f, true), ok.urls, await observe(flaky.f, true), flaky.urls]).toEqual([
      ['success', 'idle', 0, null, COVER], [URL],
      ['success', 'idle', 0, null, COVER], [URL, URL],
    ])
  })

  it('two 5xx: gives up after the one retry; a 4xx is never retried', async () => {
    const down = fetcher([503, 500, 200])
    const bad = fetcher([400, 200])
    expect([await observe(down.f, true), down.urls.length, await observe(bad.f, true), bad.urls.length]).toEqual([
      ['error', 'idle', 2, ['CoverError', 'boom 500'], null], 2,
      ['error', 'idle', 1, ['CoverError', 'boom 400'], null], 1,
    ])
  })

  it('coverRetry', () => {
    const e = (s: number) => new CoverError('x', s)
    expect([coverRetry(0, e(503)), coverRetry(1, e(503)), coverRetry(0, e(404)), coverRetry(0, new Error('net'))]).toEqual([true, false, false, false])
  })
})

// Intent (hover, focus) starts the cover; the click awaits the same fetch. However many hovers, one request
// per (scan, path, filter), and the click after them sends nothing more.
describe('prefetch on intent: deduped, then the click reuses it', () => {
  const view = { date: 'D', path: '', q: 'tomat', qs: 'simple' }
  const cover = (path: string, items: FilterCover['items'], complete = true): FilterCover =>
    ({ date: 'D', path, q: 'tomat', items, roots: { n: items.length, b: 0, o: 0 }, complete, looked: 0, unchecked: 0, ...(complete ? {} : { reason: 'Too many matches to act on at once (70,390); narrow the search or open a folder below. Agents can bulk-assign via the API.' }) })
  const item = (path: string) => ({ path, kind: 'dir' as const, b: 1, o: 1, roots: 1 })
  /** A slow fetcher (the hover's request is still in flight when the next intent lands), answering per path. */
  const fetcher = (byPath: Record<string, FilterCover>) => {
    const urls: string[] = []
    const f = async (url: string) => {
      urls.push(url)
      await new Promise(r => setTimeout(r, 5))
      const path = new URL(url, 'http://x').searchParams.get('path')!
      return new Response(JSON.stringify(byPath[path]), { status: 200 })
    }
    return { f, urls }
  }
  const url = (path: string) => `/api/filter-cover?cv=${COVER_V}&date=D&path=${encodeURIComponent(path)}&q=tomat&qs=simple`

  it('three hovers (two while the first is in flight, one after) and a click: one request', async () => {
    const qc = new QueryClient()
    const { f, urls } = fetcher({ '': cover('', [item('b/x')]) })
    const a = prefetchCover(qc, f, 'primary', view), b = prefetchCover(qc, f, 'primary', view)
    await Promise.all([a, b])
    await prefetchCover(qc, f, 'primary', view)
    const got = await fetchCover(qc, f, 'primary', view)
    expect([urls, got.items]).toEqual([[url('')], [item('b/x')]])
  })
  it('a click while the hover\'s fetch is in flight awaits it (no second request)', async () => {
    const qc = new QueryClient()
    const { f, urls } = fetcher({ '': cover('', [item('b/x')]) })
    void prefetchCover(qc, f, 'primary', view)
    const got = await fetchCover(qc, f, 'primary', view)
    expect([urls, got.items.length]).toEqual([[url('')], 1])
  })
  it('a row: the view\'s cover sliced when cached and complete (no request), else the row\'s own (`path=row`), once', async () => {
    const qc = new QueryClient()
    const { f, urls } = fetcher({
      '': cover('', [item('b/x/tomat'), item('b/xy/tomat'), item('c/tomat')]),
      'b/x': cover('b/x', [item('b/x/tomat')]),
    })
    await fetchCover(qc, f, 'primary', view)
    expect(await rowCover(qc, f, 'primary', view, 'b/x')).toEqual({ items: [item('b/x/tomat')], complete: true })
    expect(urls).toEqual([url('')])
    const qc2 = new QueryClient()
    const g = fetcher({ 'b/x': cover('b/x', [item('b/x/tomat')]) })
    const [r1, r2] = await Promise.all([rowCover(qc2, g.f, 'primary', view, 'b/x'), rowCover(qc2, g.f, 'primary', view, 'b/x')])
    expect([r1.items, r2.items, g.urls]).toEqual([[item('b/x/tomat')], [item('b/x/tomat')], [url('b/x')]])
  })
  it('a row under an over-cap view asks for its own cover (which may be under the cap)', async () => {
    const qc = new QueryClient()
    const { f, urls } = fetcher({ '': cover('', [], false), 'c': cover('c', [item('c/tomat')]) })
    await fetchCover(qc, f, 'primary', view)
    expect([(await rowCover(qc, f, 'primary', view, 'c')).items, urls]).toEqual([[item('c/tomat')], [url(''), url('c')]])
  })
  it('several rows (the selection): complete only when every row is', async () => {
    const r = (complete: boolean, paths: string[], reason?: string) => async () => ({ items: paths.map(item), complete, ...(reason ? { reason } : {}) })
    const by: Record<string, () => Promise<{ items: ReturnType<typeof item>[]; complete: boolean; reason?: string }>> = { a: r(true, ['a/1']), b: r(true, ['b/1', 'b/2']), c: r(false, [], 'too many') }
    expect(await rowsCover(p => by[p](), ['a', 'b'])).toEqual({ items: [item('a/1'), item('b/1'), item('b/2')], complete: true })
    expect(await rowsCover(p => by[p](), ['a', 'c'])).toEqual({ items: [], complete: false, reason: 'too many' })
    expect(await rowsCover(p => by[p](), ['c', 'a'])).toEqual({ items: [], complete: false, reason: 'too many' })
  })
})
