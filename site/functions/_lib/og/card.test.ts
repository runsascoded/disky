import { describe, expect, it } from 'vitest'
import { cardSvg, clip, fmtB, fmtSI, type CardData } from './card'

const T = 2 ** 40
const data = (tier: 'anon' | 'full'): CardData => ({
  tier,
  site: 'marin GCS',
  title: 'marin-us-central2/checkpoints',
  subtitle: 'scan 2026-10-02',
  total: '51.1 TiB · 179,327,698 objects',
  tiles: [
    { name: 'secret-run-a', b: 30 * T, color: '#4269d0', kids: [{ name: 'step-1000', b: 20 * T, color: '#4269d0' }, { name: 'step-2000', b: 10 * T, color: '#efb118' }] },
    { name: 'secret-run-b', b: 15 * T, color: '#efb118' },
    { name: 'tiny', b: 0.001 * T, color: '#9498a0' },
  ],
  legend: [{ label: 'Grace', color: '#4269d0', b: 30 * T }, { label: 'Hedy', color: '#efb118', b: 25 * T }],
})

const texts = (svg: string) => [...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1])
const count = (svg: string, cls: string) => (svg.match(new RegExp(`class="${cls}"`, 'g')) ?? []).length

describe('cardSvg', () => {
  it('shows the root title once, while keeping the site name on drill cards', () => {
    const root = { ...data('anon'), site: 'Public R2 Treemap Demo', title: 'Public R2 Treemap Demo' }
    expect(texts(cardSvg(root))).toEqual([
      'Public R2 Treemap Demo', 'scan 2026-10-02', '51.1 TiB · 179,327,698 objects',
    ])
    const svg = cardSvg(root)
    expect([...svg.matchAll(/<text x="40" y="([^"]+)"[^>]*font-size="([^"]+)"[^>]*>/g)].map(m => [m[1], m[2]]))
      .toEqual([['60', '32'], ['92', '20'], ['598', '24']])
  })
  it('anon: shapes only; the text is the header and the total, nothing naming a child or a person', () => {
    const svg = cardSvg(data('anon'))
    expect([texts(svg), count(svg, 'tile'), count(svg, 'kid')]).toEqual([
      ['marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02', '51.1 TiB · 179,327,698 objects'],
      3, 2,
    ])
  })
  it('full: tiles labelled at every depth where they fit, owners named in the legend', () => {
    const svg = cardSvg(data('full'))
    expect([texts(svg), count(svg, 'tile'), count(svg, 'kid')]).toEqual([
      ['marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02', 'step-1000 20.0 TiB', 'step-2000 10.0 TiB', 'secret-run-a 30.0 TiB', 'secret-run-b 15.0 TiB', '51.1 TiB · 179,327,698 objects', 'Grace 30.0 TiB', 'Hedy 25.0 TiB'],
      3, 2,
    ])
  })
  describe('nested to any depth', () => {
    const deep = (tier: 'anon' | 'full'): CardData => ({
      ...data(tier), legend: undefined,
      tiles: [{
        name: 'run', b: 100 * T, color: '#4269d0', kids: [{
          name: 'ckpt', b: 99 * T, color: '#4269d0', kids: [
            { name: 'step-1', b: 60 * T, color: '#4269d0', kids: [{ name: 'shard-0', b: 40 * T, color: '#4269d0' }, { name: 'shard-1', b: 20 * T, color: '#efb118' }] },
            { name: 'step-2', b: 39 * T, color: '#efb118' },
          ],
        }, { name: 'x', b: 1 * T, color: '#9498a0' }],
      }],
    })
    /** `[text, font size]` per label inside the treemap box (the header and footer excluded). */
    const labels = (svg: string) => [...svg.matchAll(/<text x="[^"]+" y="([^"]+)"[^>]*font-size="([^"]+)"[^>]*>([^<]*)<\/text>/g)]
      .filter(m => Number(m[1]) > 144 && Number(m[1]) < 560).map(m => [m[3], Number(m[2])])
    it('anon: every level drawn, none labelled', () => {
      const svg = cardSvg(deep('anon'))
      expect([texts(svg), count(svg, 'tile'), count(svg, 'kid')]).toEqual([
        ['marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02', '51.1 TiB · 179,327,698 objects'],
        1, 6,
      ])
    })
    it('full: a header per level where it fits, smaller deeper; a cell too narrow stays unlabelled', () => {
      const svg = cardSvg(deep('full'))
      expect([labels(svg), count(svg, 'tile'), count(svg, 'kid')]).toEqual([
        [
          ['shard-0 40.0 TiB', 11], ['shard-1 20.0 TiB', 11], ['step-1 60.0 TiB', 12], ['step-2 39.0 TiB', 12],
          ['ckpt 99.0 TiB', 13], ['run 100 TiB', 15],
        ],
        1, 6,
      ])
    })
  })
  it('an explicitly public map labels both levels without a signed tier', () => {
    const svg = cardSvg({ ...data('anon'), publicLabels: true })
    expect(texts(svg)).toEqual([
      'marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02',
      'step-1000 20.0 TiB', 'step-2000 10.0 TiB', 'secret-run-a 30.0 TiB',
      'secret-run-b 15.0 TiB', '51.1 TiB · 179,327,698 objects', 'Grace 30.0 TiB', 'Hedy 25.0 TiB',
    ])
  })
  it('heatmap: anon draws the grid with no names; full names rows, columns and cells', () => {
    const grid = { rows: ['Katherine', 'Edsger'], cols: ['Katherine', 'Edsger', 'Alan'], cells: [[0, 0, 178 * T], [1, 1, 50 * T], [1, 2, 0.02 * T]] as [number, number, number][] }
    const d = (tier: 'anon' | 'full') => ({ ...data(tier), title: 'assigner × assignee', tiles: [], legend: undefined, grid })
    const anon = cardSvg(d('anon'))
    const full = cardSvg(d('full'))
    expect([texts(anon), count(anon, 'cell'), count(anon, 'hot'), texts(full)]).toEqual([
      ['marin GCS', 'assigner × assignee', 'scan 2026-10-02', '51.1 TiB · 179,327,698 objects'],
      6, 3,
      ['marin GCS', 'assigner × assignee', 'scan 2026-10-02', 'Katherine', 'Edsger', 'Alan', 'Katherine', 'Edsger', '178 TiB', '50.0 TiB', '20.5 GiB', '51.1 TiB · 179,327,698 objects'],
    ])
  })
  it('nothing to draw: the empty note', () => {
    expect(texts(cardSvg({ ...data('anon'), tiles: [], empty: 'no matches' }))).toEqual(['marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02', 'no matches', '51.1 TiB · 179,327,698 objects'])
  })
  it('escapes, clips, formats', () => {
    expect([
      texts(cardSvg({ ...data('anon'), title: 'a<b>&"c"' }))[1],
      clip('abcdefghijklmnopqrstuvwxyz', 100, 15),
      clip('abc', 100, 15),
      clip('abc', 10, 15),
      [0, 512, 2048, 1.5 * 2 ** 30, 51.1 * T, 123.4 * T, 3100 * T].map(fmtB),
      [0, 512, 2048, 1.5e9, 15.4e12, 253e12, 3.1e15].map(fmtSI),
    ]).toEqual([
      'a&lt;b&gt;&amp;&quot;c&quot;',
      'abcdefghij…',
      'abc',
      '',
      ['0 B', '512 B', '2.00 KiB', '1.50 GiB', '51.1 TiB', '123 TiB', '3.03 PiB'],
      ['0 B', '512 B', '2.05 KB', '1.50 GB', '15.4 TB', '253 TB', '3.10 PB'],
    ])
  })
})
