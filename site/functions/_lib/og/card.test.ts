import { describe, expect, it } from 'vitest'
import { cardSvg, clip, fmtB, type CardData } from './card'

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
  it('anon: shapes only; the text is the header and the total, nothing naming a child or a person', () => {
    const svg = cardSvg(data('anon'))
    expect([texts(svg), count(svg, 'tile'), count(svg, 'kid')]).toEqual([
      ['marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02', '51.1 TiB · 179,327,698 objects'],
      3, 2,
    ])
  })
  it('full: tiles labelled where they fit, owners named in the legend', () => {
    const svg = cardSvg(data('full'))
    expect([texts(svg), count(svg, 'tile'), count(svg, 'kid')]).toEqual([
      ['marin GCS', 'marin-us-central2/checkpoints', 'scan 2026-10-02', 'secret-run-a 30.0 TiB', 'secret-run-b 15.0 TiB', '51.1 TiB · 179,327,698 objects', 'Grace 30.0 TiB', 'Hedy 25.0 TiB'],
      3, 2,
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
    ]).toEqual([
      'a&lt;b&gt;&amp;&quot;c&quot;',
      'abcdefghij…',
      'abc',
      '',
      ['0 B', '512 B', '2.00 KiB', '1.50 GiB', '51.1 TiB', '123 TiB', '3.03 PiB'],
    ])
  })
})
