import { describe, expect, it } from 'vitest'
import { compactSpans, coveredSecs, phaseFamilies, phaseFamily, phaseMatches, phaseOf, type PhaseSpan } from './scanRunsModel'
import { colorer, familyShade } from './scanRunsUi'

const span = (phase: string, start: number, end: number, lane = 0): PhaseSpan => ({ phase, start, end, status: 'done', note: null, lane })
const cut = (s: PhaseSpan & { filled?: true }) => [s.phase, s.start, s.end, s.lane, !!s.filled]

describe('phase families: the per-bucket phases as one legend item and hue', () => {
  it('groups `list <bucket>` and `import <bucket>`, keeping every other phase its own', () => {
    expect(['list a', 'import a', 'list-bucket', 'index', 'listing', 'list', 'import b'].map(phaseFamily))
      .toEqual(['list', 'import', 'list-bucket', 'index', 'listing', 'list', 'import'])
    expect(phaseFamilies(['bucket-check', 'list a', 'list b', 'import a', 'index', 'import b'])).toEqual([
      { family: 'bucket-check', members: ['bucket-check'] },
      { family: 'list', members: ['list a', 'list b'] },
      { family: 'import', members: ['import a', 'import b'] },
      { family: 'index', members: ['index'] },
    ])
  })
  it('a legend key matches its own phase, or every member of its family', () => {
    expect([phaseOf('list', 'list a'), phaseOf('list', 'import a'), phaseOf('list a', 'list a'), phaseOf('list a', 'list b'), phaseOf('index', 'index')])
      .toEqual([true, false, true, false, true])
  })
  it('colors a family\'s members as shades of its hue, a single phase its plain hue', () => {
    const c = colorer(['bucket-check', 'list a', 'list b', 'list c', 'index'])
    expect(['bucket-check', 'list', 'list a', 'list b', 'list c', 'index', 'list z'].map(c)).toEqual([
      'var(--s1)',
      'var(--s2)',
      'color-mix(in oklab, var(--s2), black 35%)',
      'var(--s2)',
      'color-mix(in oklab, var(--s2), white 35%)',
      'var(--s4)',
      // a member the order didn't name takes its family's hue
      'var(--s2)',
    ])
    expect([familyShade('var(--s5)', 0, 1), familyShade('var(--s5)', 1, 4)]).toEqual(['var(--s5)', 'color-mix(in oklab, var(--s5), black 12%)'])
  })
})

describe('phaseMatches: the page\'s `?ph=` filter', () => {
  it('passes a case-insensitive substring of the phase or its family; empty passes all', () => {
    const phases = ['list marin-us-east-08a', 'import hero-checkpoints', 'static-names', 'interval-store']
    expect([
      phases.filter(p => phaseMatches('', p)),
      phases.filter(p => phaseMatches(undefined, p)),
      phases.filter(p => phaseMatches('LIST', p)),
      phases.filter(p => phaseMatches('08a', p)),
      phases.filter(p => phaseMatches(' st', p)),
      phases.filter(p => phaseMatches('names', p)),
    ]).toEqual([phases, phases, ['list marin-us-east-08a'], ['list marin-us-east-08a'], ['list marin-us-east-08a', 'static-names', 'interval-store'], ['static-names']])
  })
})

describe('compactSpans: the main lane, with overlapped phases where it has none', () => {
  it('fills a stretch only an overlapped phase covers (static-names running on after the main sequence)', () => {
    // 2026-11-17T0602: the main lane ends at interval-store; static-names ran on until 09:56
    const spans = [span('list', 0, 2400), span('index', 2400, 3500), span('static-names', 3500, 14000, 1), span('interval-store', 3600, 5000)]
    expect(compactSpans(spans).map(cut)).toEqual([
      ['list', 0, 2400, 0, false],
      ['index', 2400, 3500, 0, false],
      ['static-names', 3500, 3600, 1, true],
      ['interval-store', 3600, 5000, 0, false],
      ['static-names', 5000, 14000, 1, true],
    ])
  })
  it('fills from the lowest lane first, and leaves uncovered time uncovered', () => {
    const spans = [span('a', 0, 10), span('b', 5, 30, 1), span('c', 20, 40, 2), span('d', 50, 60)]
    expect(compactSpans(spans).map(cut)).toEqual([
      ['a', 0, 10, 0, false],
      ['b', 10, 30, 1, true],
      ['c', 30, 40, 2, true],
      ['d', 50, 60, 0, false],
    ])
  })
  it('draws only the main lane when it covers everything', () => {
    const spans = [span('a', 0, 10), span('b', 2, 8, 1), span('c', 10, 20)]
    expect(compactSpans(spans).map(cut)).toEqual([['a', 0, 10, 0, false], ['c', 10, 20, 0, false]])
  })
  it('a family\'s parallel time counts once', () => {
    expect(coveredSecs([span('list a', 0, 100), span('list b', 10, 60, 1), span('list c', 150, 200, 2)])).toBe(150)
  })
})
