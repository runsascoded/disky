import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TreeNode } from './types'
import { type CoverItem, type RowMatches, rowMatchesOf } from './filterCover'

// The table's own logic under a filter, with its surroundings stubbed: the selection is preset (`selected`),
// and the trash controls render what they would send.
const { selected, staged, acts, planItems } = vi.hoisted(() => ({ selected: new Set<string>(), staged: [] as string[][], acts: [] as { resolve: () => Promise<unknown> }[], planItems: [] as { prefix: string; kind?: 'prefix' | 'object' }[] }))
vi.mock('./auth', () => ({ useCanStage: () => true }))
vi.mock('./store', () => ({ useStore: () => ({ staging: true }) }))
vi.mock('./plans', () => ({
  useStage: () => ({ error: null, mutate: (v: { prefixes: string[] }) => staged.push(v.prefixes) }),
  // The open plan (`useStagedIndex`): `planItems`, preset per test.
  useStagedPlan: () => ({ data: { items: planItems } }),
}))
vi.mock('./perf', () => ({ usePerfCommit: () => {} }))
vi.mock('./units', () => ({ useUnits: () => ({ fmtBytes: (b: number) => `${b} B` }) }))
vi.mock('use-prms', () => ({ intParam: () => ({}), codeParam: () => ({}), enumParam: () => ({}), useUrlState: () => [20, () => {}] }))
vi.mock('./largest', () => ({ LARGEST_MAX: 500, LARGEST_STEP: 100, flatRows: () => [], useLargest: () => ({ data: undefined }) }))
vi.mock('./Tooltip', () => ({ Tooltip: ({ content, children, placement }: { content: ReactNode; children: ReactNode; placement?: string }) => createElement('span', { 'data-tip': typeof content === 'string' ? content : '', 'data-placement': placement }, children) }))
// A filtered row's controls as a marker naming what it acts on; the selection's action records its deps.
vi.mock('./MatchActions', async () => {
  const { createElement: h } = await import('react')
  return {
    RowActs: ({ path, src }: { path: string; src: { key: string } }) => h('i', { 'data-row': path, 'data-src': src.key }),
    ActStatus: () => h('span', { className: 'act-live' }),
    useActDeps: () => ({}),
    useKeepFocus: () => ({ ref: () => {}, handlers: {} }),
    useMatchAct: (deps: { resolve: () => Promise<unknown> }) => { acts.push(deps); return { state: { s: 'idle' }, confirmT: null, start: async () => {} } },
  }
})
vi.mock('./rowSelection', () => ({
  useRowSelectionKeys: () => {},
  useRowSelection: <T,>(rows: readonly T[], key: (r: T) => string) => ({
    selected, isSelected: (r: T) => selected.has(key(r)), toggle: () => {}, rowRef: () => () => {}, rowProps: () => ({}),
    pageAll: false, togglePage: () => {}, clear: () => {}, cursor: 0, cursorRow: rows[0],
  }),
}))
const { ChildrenTable } = await import('./ChildrenTable')

// The fleet root under `?f=tomat`: `marin-eu-west4` holds one match (`tomat`, drawn as a chain), the show
// folder 3 matching files among others (drawn as its own subtree), `bkt-none` no listed match.
const node: TreeNode = {
  n: 'all', k: 'dir', b: 0, o: 0, c: [
    { n: 'marin-eu-west4', k: 'dir', b: 50, o: 2, c: [{ n: 'tomat', k: 'dir', b: 50, o: 2, c: [{ n: 'a', k: 'file', b: 20, o: 1 }, { n: 'b', k: 'file', b: 30, o: 1 }] }] },
    { n: 'marin-us-central1', k: 'dir', b: 300, o: 3, c: [{ n: 'show', k: 'dir', b: 300, o: 3, c: [{ n: 'x1.mp3', k: 'file', b: 100, o: 1 }, { n: 'x2.mp3', k: 'file', b: 100, o: 1 }, { n: 'x3.mp3', k: 'file', b: 100, o: 1 }] }] },
    { n: 'bkt-none', k: 'dir', b: 7, o: 1 },
  ],
}
const items: CoverItem[] = [
  { path: 'marin-eu-west4/tomat', kind: 'dir', b: 50, o: 2, roots: 1 },
  ...[1, 2, 3].map(i => ({ path: `marin-us-central1/show/x${i}.mp3`, kind: 'file' as const, b: 100, o: 1, roots: 1 })),
]
/** The filter's row source: `resolve` slices `items` to the row (recording each row asked for). */
const asked: string[] = []
const src = (items: CoverItem[]) => ({
  key: 'K', prefetch: async () => {},
  resolve: async (row: string) => { asked.push(row); return { items: items.filter(i => i.path === row || i.path.startsWith(`${row}/`)), complete: true } },
})
/** Each row's exact match roots: the response's full list (every root of `items`). */
const exact = rowMatchesOf(items, 'exact')
const render = (filter?: ReturnType<typeof src> & { blocked?: string }, rowMatches: ((row: string) => RowMatches | null) | undefined = exact) => renderToStaticMarkup(createElement(ChildrenTable, {
  node, segs: [], scheme: 'gs://', onOpen: () => {}, onOpenObject: () => {}, filter, rowMatches,
}))
const rows = (html: string) => [...(html.match(/<tbody>(.*?)<\/tbody>/)?.[1] ?? '').matchAll(/<tr[^>]*>(.*?)<\/tr>/g)].map(([, r]) => r)
const name = (row: string) => /<td class="prefix">(.*?)<\/td>/.exec(row)![1].replace(/<[^>]+>/g, '')
const tip = (row: string) => /<td class="prefix"><span data-tip="(.*?)"/.exec(row)?.[1]
const checkboxes = (html: string) => rows(html).map(r => /<td class="col-sel">(.*?)<\/td>/.exec(r)?.[1] === '' ? 'no box' : 'box')
const actionsCell = (row: string) => /<td class="actions">(.*)<\/td>$/.exec(row)?.[1] ?? ''

beforeEach(() => { selected.clear(); staged.length = 0; acts.length = 0; asked.length = 0; planItems.length = 0 })

describe('the children table under a filter', () => {
  it('a row holding exactly one match shows the path to it (as its tile); several: its name and how many; none: its name', () => {
    // At the root no name is elided, so no row carries the full-path tooltip (it would repeat the cell).
    // `marin-us-central1` draws one chain (`show`) but holds 3 matches: never labelled as `…/show`.
    expect(rows(render(src(items))).map(r => [name(r), tip(r)])).toEqual([
      ['marin-us-central1 · 3 matches', undefined],
      ['marin-eu-west4/tomat', undefined],
      ['bkt-none', undefined],
    ])
  })
  it('the label never rests on the drawn tree: a lone drawn child with more matches undrawn (gcs `marin-us-east5`), or an unknown count, is the row\'s name', () => {
    // As on gcs prod: `marin-eu-west4` drew only `tomat`, but held 9 match roots.
    const nine = (row: string) => (row === 'marin-eu-west4' ? { n: 9 } : exact(row))
    const label = (html: string) => rows(html).map(r => /<td class="prefix">(.*?)<\/td>/.exec(r)![1].replace(/<\/?a[^>]*>/g, ''))
    // A capped list (gcs prod's fleet root): a lower bound for a row listing several; one listed says nothing.
    const capped = rowMatchesOf([...items, { path: 'marin-eu-west4/x.parquet' }], 'capped')
    expect([label(render(src(items), nine)), label(render(src(items), () => null)), label(render(src(items))), label(render(src(items), capped))]).toEqual([
      ['marin-us-central1<span class="row-n"> · 3 matches</span>', 'marin-eu-west4<span class="row-n"> · 9 matches</span>', 'bkt-none'],
      ['marin-us-central1', 'marin-eu-west4', 'bkt-none'],
      ['marin-us-central1<span class="row-n"> · 3 matches</span>', 'marin-eu-west4/tomat', 'bkt-none'],
      ['marin-us-central1<span class="row-n"> · 3+ matches</span>', 'marin-eu-west4<span class="row-n"> · 2+ matches</span>', 'bkt-none'],
    ])
  })
  it('every row offers its actions at once over its own path (no "list" step), and every row is selectable', () => {
    const html = render(src(items))
    expect(rows(html).map(r => [/data-row="([^"]*)" data-src="([^"]*)"/.exec(actionsCell(r))?.slice(1)])).toEqual([
      [['marin-us-central1', 'K']],
      [['marin-eu-west4', 'K']],
      [['bkt-none', 'K']],
    ])
    expect([checkboxes(html), html.includes('>list<')]).toEqual([['box', 'box', 'box'], false])
  })
  it('a view whose matches can\'t be acted on (`blocked`: a refusal, rollup, catalog or approximate answer): no row offers an action, nothing selects', () => {
    selected.add('gs://marin-eu-west4')
    const html = render({ ...src(items), blocked: 'per-bucket totals only' })
    expect([
      rows(html).map(r => [name(r), r.includes('class="col-sel"'), r.includes('class="actions"'), r.includes('data-row=')]),
      html.includes('class="sel-bar"'),
    ]).toEqual([
      [['marin-us-central1 · 3 matches', false, false, false], ['marin-eu-west4/tomat', false, false, false], ['bkt-none', false, false, false]],
      false,
    ])
  })
  it('the selection acts on the selected rows\' matches (resolved on the click), never their whole prefixes', async () => {
    selected.add('gs://marin-eu-west4').add('gs://marin-us-central1')
    const html = render(src(items))
    // The bar: the count, then trash over the rows' matches (no per-match count before the click).
    expect(/<div class="sel-bar-dock">(.*)<\/div><\/section>$/.exec(html)![1].replace(/<svg.*?<\/svg>/, '<svg/>')).toBe(
      '<span class="sel-bar"><b>2</b> selected · 350 B<span class="acts"><span class="mact sel-acts" tabindex="-1">'
      + '<span data-tip="Optional: one note for this deletion — why these matches go. Stored with the batch, visible to the admin who dispatches."><input class="memo" placeholder="note (optional)" aria-label="deletion note" value=""/></span>'
      + '<span data-tip=""><button type="button" class="trash" aria-label="trash selected"><svg/> trash</button></span><span class="act-live"></span></span>'
      + '<button type="button" class="quiet">deselect</button></span></span>')
    const got = await acts.at(-1)!.resolve()
    expect([asked, got]).toEqual([
      ['marin-us-central1', 'marin-eu-west4'],
      { items: [...items.slice(1), items[0]], complete: true },
    ])
  })
  it('without a filter the root\'s rows (whole buckets) offer no action, each named as itself', () => {
    selected.add('gs://bkt-none')
    const html = render()
    expect([rows(html).map(name), rows(html).map(r => r.includes('class="actions"')), html.includes('class="sel-bar"')]).toEqual([
      ['marin-us-central1', 'marin-eu-west4', 'bkt-none'], [false, false, false], false,
    ])
  })
  it('without a filter a file row acts on that exact object (never `key/`), a folder row on its prefix', () => {
    const show = (node.c![1].c![0])
    const html = renderToStaticMarkup(createElement(ChildrenTable, {
      node: { ...show, c: [...show.c!, { n: 'sub', k: 'dir', b: 1, o: 1 }] }, segs: ['marin-us-central1', 'show'], scheme: 'gs://',
      onOpen: () => {}, onOpenObject: () => {},
    }))
    expect(rows(html).map(r => /data-tip="(Stage[^"]*)"/.exec(actionsCell(r))?.[1])).toEqual([
      ...Array(3).fill('Stage this file for deletion (exactly this object) — an admin approves and dispatches from /staged'),
      'Stage this folder for deletion — an admin approves and dispatches from /staged',
    ])
    expect(rows(html).map(r => /data-tip="Stage[^"]*" data-placement="([^"]*)"/.exec(actionsCell(r))?.[1])).toEqual([
      'right', 'right', 'right', 'right',
    ])
  })
  it('rows the open plan holds are marked: staged itself or under a staged folder (a chip in place of the trash), a folder holding staged items counts them', () => {
    const show = (node.c![1].c![0])
    planItems.push({ prefix: 'gs://marin-us-central1/show/x1.mp3', kind: 'object' }, { prefix: 'gs://marin-us-central1/show/sub/' }, { prefix: 'gs://marin-us-central1/show/keep/deep/' })
    const html = renderToStaticMarkup(createElement(ChildrenTable, {
      node: { ...show, c: [...show.c!, { n: 'sub', k: 'dir', b: 1, o: 1 }, { n: 'keep', k: 'dir', b: 1, o: 1 }] }, segs: ['marin-us-central1', 'show'], scheme: 'gs://',
      onOpen: () => {}, onOpenObject: () => {},
    }))
    expect(rows(html).map(r => [name(r), /<a class="staged-chip"[^>]*>([^<]*)<\/a>/.exec(actionsCell(r))?.[1] ?? (/aria-label="trash"/.test(actionsCell(r)) ? 'trash' : '?')])).toEqual([
      ['x1.mp3', 'staged'], ['x2.mp3', 'trash'], ['x3.mp3', 'trash'], ['sub', 'staged'], ['keep · 1 staged', 'trash'],
    ])
    expect([...html.matchAll(/<tr([^>]*)>/g)].map(([, a]) => /class="([^"]*)"/.exec(a)?.[1] ?? '')).toEqual(['', 'staged', '', '', 'staged', '', 'total-row'])
  })
})
