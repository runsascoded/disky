import { describe, expect, it } from 'vitest'
import { dimUnmatched, filterNodes, parseQuery } from '../src/filter'

const PATHS = ['src/App.tsx', 'src/api.ts', 'tests/App.test.tsx', 'README.md']

describe('parseQuery', () => {
  it('matches everything for an empty or whitespace query', () => {
    expect(PATHS.filter(parseQuery(''))).toEqual(PATHS)
    expect(PATHS.filter(parseQuery('   '))).toEqual(PATHS)
  })

  it('does substring matching, case-insensitively by default', () => {
    expect(PATHS.filter(parseQuery('app'))).toEqual(['src/App.tsx', 'tests/App.test.tsx'])
    expect(PATHS.filter(parseQuery('.TS'))).toEqual(['src/App.tsx', 'src/api.ts', 'tests/App.test.tsx'])
  })

  it('honors caseSensitive', () => {
    expect(PATHS.filter(parseQuery('app', { caseSensitive: true }))).toEqual([])
    expect(PATHS.filter(parseQuery('App', { caseSensitive: true }))).toEqual([
      'src/App.tsx',
      'tests/App.test.tsx',
    ])
  })

  it('treats /…/ as a regex', () => {
    expect(PATHS.filter(parseQuery('/^src\\//'))).toEqual(['src/App.tsx', 'src/api.ts'])
    expect(PATHS.filter(parseQuery('/\\.tsx$/'))).toEqual(['src/App.tsx', 'tests/App.test.tsx'])
  })

  it('applies inline regex flags on top of the case default', () => {
    expect(PATHS.filter(parseQuery('/APP/'))).toEqual(['src/App.tsx', 'tests/App.test.tsx'])
    expect(PATHS.filter(parseQuery('/APP/', { caseSensitive: true }))).toEqual([])
    expect(PATHS.filter(parseQuery('/APP/i', { caseSensitive: true }))).toEqual([
      'src/App.tsx',
      'tests/App.test.tsx',
    ])
  })

  it('degrades a half-typed regex to a substring match instead of throwing', () => {
    // Typing "/src/(" — an unterminated group would throw on every keystroke.
    expect(() => parseQuery('/src/(')).not.toThrow()
    expect(PATHS.filter(parseQuery('/src/('))).toEqual([])
    expect(PATHS.filter(parseQuery('/(/'))).toEqual([])
  })

  it('matches a literal slash-wrapped substring when the regex is invalid', () => {
    const paths = ['a/(/b', 'plain']
    expect(paths.filter(parseQuery('/(/'))).toEqual(['a/(/b'])
  })
})

describe('filterNodes', () => {
  it('keeps nodes whose extracted path matches', () => {
    const nodes = PATHS.map(p => ({ p }))
    expect(filterNodes(nodes, n => n.p, 'src').map(n => n.p)).toEqual(['src/App.tsx', 'src/api.ts'])
  })

  it('returns every node for an empty query', () => {
    const nodes = PATHS.map(p => ({ p }))
    expect(filterNodes(nodes, n => n.p, '').length).toBe(4)
  })
})

describe('dimUnmatched', () => {
  it('leaves matching cells alone so the lens can no-op', () => {
    expect(dimUnmatched({ bg: 'red' }, true)).toBeNull()
  })

  it('dims non-matching cells while preserving the resolved style', () => {
    expect(dimUnmatched({ bg: 'red', ink: '#fff' }, false)).toEqual({
      bg: 'red',
      ink: '#fff',
      opacity: 0.15,
    })
  })

  it('honors an opacity override', () => {
    expect(dimUnmatched({ bg: 'red' }, false, { opacity: 0.4 })).toEqual({ bg: 'red', opacity: 0.4 })
  })
})
