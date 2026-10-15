import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { cutoffDays, fmtCutoff } from './federatedPickerModel'
import { encodeCutoff, parseCutoff } from './federatedScans'
import { scanAtInstant } from './scan'
import { UnionMissingNote } from './UnionNotes'
import { cutoffOfScan, marksOf, missingSources, parseUnionScans, pickCutoff, refusedSources, rootSource, scanAtCutoff, scopedPath, seriesMissing, sourcesBefore, UnionMarks } from './unionSources'

// r2's shape (specs/federated-scans.md, "First live cases" 1): the primary and `/meta`, two stores of one
// deployment that share scan ids (date-only, at their midnights); meta has an icon, the primary none.
const body = { union: { view: 'all', sources: [
  { store: 'primary', label: 'Public R2 buckets', scans: [
    { id: '2026-10-14', at: '2026-10-14T00:00:00.000Z', day: true as const },
    { id: '2026-10-13', at: '2026-10-13T00:00:00.000Z', day: true as const },
  ] },
  { store: 'meta', label: 'Index', icon: '/icons/meta.svg', scans: [
    { id: '2026-10-14', at: '2026-10-14T00:00:00.000Z', day: true as const },
  ] },
] } }
const u = parseUnionScans(body)!
const marks = marksOf(u)

describe('union picker sources', () => {
  it('`/api/union-scans` → picker sources; `{union: null}` → none (the single-store picker)', () => {
    expect(u).toEqual({ view: 'all', sources: [
      { source: 'primary', label: 'Public R2 buckets', scans: [
        { id: '2026-10-14', revision: '', observed_through: '2026-10-14T00:00:00.000Z', published_at: '2026-10-14T00:00:00.000Z', day: true },
        { id: '2026-10-13', revision: '', observed_through: '2026-10-13T00:00:00.000Z', published_at: '2026-10-13T00:00:00.000Z', day: true },
      ] },
      { source: 'meta', label: 'Index', icon: '/icons/meta.svg', scans: [
        { id: '2026-10-14', revision: '', observed_through: '2026-10-14T00:00:00.000Z', published_at: '2026-10-14T00:00:00.000Z', day: true },
      ] },
    ] })
    expect(parseUnionScans({ union: null })).toBe(null)
  })
  it('shared ids are one chip per instant carrying both marks (meta’s icon, the primary’s label); date-only ids read as their dates in any zone', () => {
    const want = [
      { day: '2026-10-14', events: [{ at: '2026-10-14T00:00:00.000Z', time: 'Oct 14', day: true, sources: [
        { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, { source: 'primary', label: 'Public R2 buckets' },
      ] }] },
      { day: '2026-10-13', events: [{ at: '2026-10-13T00:00:00.000Z', time: 'Oct 13', day: true, sources: [{ source: 'primary', label: 'Public R2 buckets' }] }] },
    ]
    expect([cutoffDays(u.sources, 'UTC'), cutoffDays(u.sources, 'America/New_York')]).toEqual([want, want])
  })
  it('scan id ↔ cutoff: a picked instant reads as the newest observation through it, among the page’s scans', () => {
    expect([
      cutoffOfScan(u.sources, '2026-10-13'),
      cutoffOfScan(u.sources, '2026-10-01'),
      scanAtCutoff(u.sources, '2026-10-14T00:00:00.000Z'),
      scanAtCutoff(u.sources, '2026-10-13T18:30:00.000Z'),
      scanAtCutoff(u.sources, '2026-10-12T00:00:00.000Z'),
      scanAtCutoff(u.sources, '2026-10-14T09:00:00.000Z', new Set(['2026-10-13'])),
    ]).toEqual(['2026-10-13T00:00:00.000Z', null, '2026-10-14', '2026-10-13', null, '2026-10-13'])
    expect(sourcesBefore(u.sources, '2026-10-14T00:00:00.000Z').map(s => [s.source, s.scans.map(o => o.id)])).toEqual([['primary', ['2026-10-13']], ['meta', []]])
  })
})

describe('union answers on the page', () => {
  it('a root’s store and scan (`src`/`at`); a node without them has none; an unlisted store is its key', () => {
    expect([
      rootSource({ src: 'meta', at: '2026-10-14' }, marks),
      rootSource({ src: 'primary', at: '2026-10-14' }, marks),
      rootSource({}, marks),
      rootSource({ src: 'other', at: '2026-10-14' }, marks),
    ]).toEqual([
      { mark: { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, at: '2026-10-14' },
      { mark: { source: 'primary', label: 'Public R2 buckets' }, at: '2026-10-14' },
      null,
      { mark: { source: 'other', label: 'other' }, at: '2026-10-14' },
    ])
  })
  it('a member refusing the filter is named with its reason; members answering are not', () => {
    const vector = { view: 'all', at: '2026-10-14T00:00:00.000Z', sources: [
      { store: 'primary', date: '2026-10-14' },
      { store: 'meta', date: '2026-10-14', refused: { code: 'scan-not-indexed', error: 'scan 2026-10-14 is not in the name index' } },
    ] }
    expect([refusedSources(vector, marks), refusedSources({ view: 'all', sources: [{ store: 'primary', date: '2026-10-14' }] }, marks), refusedSources(undefined, marks)]).toEqual([
      [{ mark: { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, code: 'scan-not-indexed', error: 'scan 2026-10-14 is not in the name index' }],
      [],
      [],
    ])
  })
  it('a series’ points missing a member: that member, through its last missing point', () => {
    expect(seriesMissing([
      { date: '2026-10-12', missing: ['meta'] },
      { date: '2026-10-13', missing: ['meta'] },
      { date: '2026-10-14' },
    ], marks)).toEqual([{ mark: { source: 'meta', label: 'Index', icon: '/icons/meta.svg' }, through: '2026-10-13' }])
    expect(seriesMissing([{ date: '2026-10-14' }], marks)).toEqual([])
  })
})

// `?f=` on a union root (r2.oa.dev `?f=nemotron_cc_v2`, 2026-10-14): the filtered root holds one store's one root
// (`marin-na`), which the map opens inside (`mapPath`'s lone-root rule) while the URL path stays the root. The
// series and the diff asked the union ROOT with `q=` (a 400: "drill into one store"; the chart: "fewer than two
// scans hold this path"); they must ask the lone root's path, which its member answers.
describe('the series and diff path under a filtered union root', () => {
  const one = { c: [{ n: 'marin-na' }] }
  const two = { c: [{ n: 'marin-na' }, { n: 'mb' }] }
  it('the lone root the map opens: its path; several roots, or none: the root; not loaded: wait', () => {
    expect([
      scopedPath('', { union: true, filtered: true, tree: one }),
      scopedPath('', { union: true, filtered: true, tree: two }),
      scopedPath('', { union: true, filtered: true, tree: { c: [] } }),
      scopedPath('', { union: true, filtered: true, tree: undefined }),
    ]).toEqual(['marin-na', '', '', undefined])
  })
  it('a drilled path, an unfiltered union root, a single store: the page path, as before', () => {
    expect([
      scopedPath('marin-na/x', { union: true, filtered: true, tree: one }),
      scopedPath('', { union: true, filtered: false, tree: one }),
      scopedPath('', { union: false, filtered: true, tree: one }),
      scopedPath('', { union: false, filtered: true, tree: undefined }),
    ]).toEqual(['marin-na/x', '', '', ''])
  })
})

// Cutoffs `?d=` can't say (r2 dev, 2026-10-14): meta updated at 10/13 19:35, the primary's newest by then is 10/13
// 06:07. Mapping that chip to the primary's scan read the union at 06:07, where meta reads its older scan.
const timed = parseUnionScans({ union: { view: 'all', sources: [
  { store: 'primary', label: 'Public R2 buckets', scans: [
    { id: '2026-10-13T0607', at: '2026-10-13T06:07:00.000Z' },
    { id: '2026-10-12T0600', at: '2026-10-12T06:00:00.000Z' },
  ] },
  { store: 'meta', label: 'Index', icon: '/icons/meta.svg', scans: [
    { id: '2026-10-13T1935', at: '2026-10-13T19:35:00.000Z' },
    { id: '2026-10-12T0600', at: '2026-10-12T06:00:00.000Z' },
  ] },
] } })!.sources
const primaryScans = new Set(['2026-10-13T0607', '2026-10-12T0600'])

describe('`?d=` or `?at=`: what a picked cutoff writes', () => {
  it('a cutoff one page scan says for every member: its `?d=`; one it doesn’t: the instant', () => {
    expect([
      // Both members' shared scan, and the primary's own update (meta carries its 10/12 scan either way).
      pickCutoff(timed, '2026-10-12T06:00:00.000Z', primaryScans),
      pickCutoff(timed, '2026-10-13T06:07:00.000Z', primaryScans),
      // Meta's own update: at the primary's 06:07 meta reads 10/12, so no scan id says it.
      pickCutoff(timed, '2026-10-13T19:35:00.000Z', primaryScans),
      // A cutoff before any page scan: the instant.
      pickCutoff(timed, '2026-10-11T00:00:00.000Z', primaryScans),
    ]).toEqual([
      { d: '2026-10-12T0600' },
      { d: '2026-10-13T0607' },
      { at: '2026-10-13T19:35:00.000Z' },
      { at: '2026-10-11T00:00:00.000Z' },
    ])
  })
  it('a meta scan the page lists (its id is pickable) is its `?d=`: at its instant every member reads as picked', () => {
    expect(pickCutoff(timed, '2026-10-13T19:35:00.000Z', new Set([...primaryScans, '2026-10-13T1935']))).toEqual({ d: '2026-10-13T1935' })
  })
  it('the URL form: compact UTC, ISO read too; anything else is no cutoff', () => {
    expect([
      encodeCutoff('2026-10-13T19:35:00.000Z'), encodeCutoff('2026-10-13T19:35:07.000Z'), encodeCutoff('2026-10-13T19:35:07.250Z'),
      parseCutoff('20261013T1935Z'), parseCutoff('20261013T193507Z'), parseCutoff('2026-10-13T19:35Z'), parseCutoff('2026-10-13T19:35:07.250Z'),
      parseCutoff('2026-10-13T1935'), parseCutoff('20261332T0000Z'), parseCutoff('2026-10-13'), parseCutoff(undefined),
    ]).toEqual([
      '20261013T1935Z', '20261013T193507Z', '2026-10-13T19:35:07.250Z',
      '2026-10-13T19:35:00.000Z', '2026-10-13T19:35:07.000Z', '2026-10-13T19:35:00.000Z', '2026-10-13T19:35:07.250Z',
      null, null, null, null,
    ])
  })
  it('the page’s scan at a cutoff: the newest at or before it (a date-only id at its known start)', () => {
    const scans = ['2026-10-13T0607', '2026-10-12', '2026-10-11T2300']
    const times = { '2026-10-12': '2026-10-12T0900' }
    expect([
      scanAtInstant(scans, '2026-10-13T19:35:00.000Z', times),
      scanAtInstant(scans, '2026-10-12T08:59:00.000Z', times),
      scanAtInstant(scans, '2026-10-12T09:00:00.000Z', times),
      scanAtInstant(scans, '2026-10-11T00:00:00.000Z', times),
    ]).toEqual(['2026-10-13T0607', '2026-10-11T2300', '2026-10-12', null])
  })
})

describe('a member with no scan at the cutoff', () => {
  const vector = (meta: string | null) => ({ view: 'all', at: '2026-10-11T00:00:00.000Z', sources: [{ store: 'primary', date: '2026-10-10' }, { store: 'meta', date: meta }] })
  it('is named (the map and table leave it out); members with a scan, or no union answer, name none', () => {
    expect([missingSources(vector(null), marks), missingSources(vector('2026-10-10'), marks), missingSources(undefined, marks)]).toEqual([
      [{ source: 'meta', label: 'Index', icon: '/icons/meta.svg' }],
      [],
      [],
    ])
  })
  it('the note: its mark and the cutoff, in the viewer’s zone', () => {
    const note = (meta: string | null) => renderToStaticMarkup(createElement(UnionMarks.Provider, { value: marks }, createElement(UnionMissingNote, { union: vector(meta) })))
    const when = fmtCutoff('2026-10-11T00:00:00.000Z', Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect([note(null), note('2026-10-10')]).toEqual([
      // (React's server renderer hoists a preload for the icon.)
      `<link rel="preload" as="image" href="/icons/meta.svg"/><p class="tab-note union-missing" role="status">No <span><span class="src-mark src-mark-inline"><img src="/icons/meta.svg" alt="" aria-hidden="true" draggable="false"/><span>Index</span></span></span> scan at or before ${when}: not on the map or in the table.</p>`,
      '',
    ])
  })
})
