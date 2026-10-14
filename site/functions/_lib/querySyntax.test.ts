import { describe, expect, it } from 'vitest'
import type { Matcher, ParseResult, QueryError } from './queryAst'
import { DEFAULT_SYNTAX, matcherText, parseAst, regex, resolveSyntax, simple, SYNTAXES } from './querySyntax'
import { queryParam } from './scope'
import { planNegative, planPositive } from './searchQuery'

const sub = (text: string): Matcher => ({ kind: 'sub', text })
const glob = (...pieces: string[]): Matcher => ({ kind: 'glob', pieces })
const re = (source: string): Matcher => ({ kind: 'regex', source })

describe('`simple`: parse table', () => {
  it.each<[string, Matcher[][], Matcher[]]>([
    ['tomat', [[sub('tomat')]], []],
    ['TTL', [[sub('ttl')]], []],
    ['ckpt|tmp', [[sub('ckpt')], [sub('tmp')]], []],
    ['abc def|ghi', [[sub('abc'), sub('def')], [sub('ghi')]], []],
    ['ckpt -run', [[sub('ckpt')]], [sub('run')]],
    ['-x', [[]], [sub('x')]],
    ['-x -y', [[]], [sub('x'), sub('y')]],
    ['ckpt -x*y', [[sub('ckpt')]], [glob('x', 'y')]],
    ['a*ckpt', [[glob('a', 'ckpt')]], []],
    ['ckpt*final', [[glob('ckpt', 'final')]], []],
    ['*.pt', [[glob('', '.pt')]], []],
    ['tmp/*/ckpt', [[glob('tmp/', '/ckpt')]], []],
    ['"a b"', [[sub('a b')]], []],
    ['"-draft"', [[sub('-draft')]], []],
    ['-"a b"', [[]], [sub('a b')]],
    ['"a|b"', [[sub('a|b')]], []],
    ['"x*y"', [[sub('x*y')]], []],
    ['"open', [[sub('open')]], []],
    ['ttl|', [[sub('ttl')]], []],
    ['abc | -x', [[sub('abc')], []], [sub('x')]],
    ['/ckpt.*final/', [[re('ckpt.*final')]], []],
    ['^tomat', [[{ kind: 'sub', text: 'tomat', start: true }]], []],
    ['.JSON$', [[{ kind: 'sub', text: '.json', end: true }]], []],
    ['^config.json$', [[{ kind: 'sub', text: 'config.json', start: true, end: true }]], []],
    ['^ckpt*final', [[{ kind: 'glob', pieces: ['ckpt', 'final'], start: true }]], []],
    ['ckpt -^tmp', [[sub('ckpt')]], [{ kind: 'sub', text: 'tmp', start: true }]],
    ['"^a$"', [[sub('^a$')]], []],
    ['"^"abc', [[sub('^abc')]], []],
    ['abc"$"', [[sub('abc$')]], []],
    ['a^b$c', [[sub('a^b$c')]], []],
    ['^', [[sub('^')]], []],
    ['$', [[sub('$')]], []],
    ['^$', [[sub('^$')]], []],
  ])('%s', (q, alts, neg) => {
    expect(simple.parse(q)).toEqual({ ast: { alts, neg } })
  })
  it('nothing to filter by', () => {
    expect(['', '  ', '|', '||', '""'].map(q => simple.parse(q))).toEqual([{ ast: null }, { ast: null }, { ast: null }, { ast: null }, { ast: null }])
  })
  it('errors: a positive term under 3 characters in a row (exclusions, and a lone literal, exempt), an invalid `/…/` regex', () => {
    expect(['gr x', 'ckpt gr', 'ckpt|gr', 'a*b*cd', '"ab" "cd"', 'ckpt - x', 'gr -tmp', '/a(/', '/[z-a]/'].map(q => simple.parse(q))).toEqual<ParseResult[]>([
      { error: 'type at least 3 characters (“gr”)', code: 'short-term' },
      { error: 'type at least 3 characters (“gr”)', code: 'short-term' },
      { error: 'type at least 3 characters (“gr”)', code: 'short-term' },
      { error: 'type at least 3 characters (“a*b*cd”)', code: 'short-term' },
      { error: 'type at least 3 characters (“ab”)', code: 'short-term' },
      { error: 'type at least 3 characters (“-”)', code: 'short-term' },
      { error: 'type at least 3 characters (“gr”)', code: 'short-term' },
      { error: 'invalid regex: Invalid regular expression: /a(/i: Unterminated group', code: 'invalid-regex' },
      { error: 'invalid regex: Invalid regular expression: /[z-a]/i: Range out of order in character class', code: 'invalid-regex' },
    ])
  })
  it('a lone literal has no minimum (the static drilldown answers it): one or two characters, quoted or not', () => {
    expect(['gr', '"ab"', '.', 'A', ' x '].map(q => simple.parse(q))).toEqual<ParseResult[]>(
      [[sub('gr')], [sub('ab')], [sub('.')], [sub('a')], [sub('x')]].map(t => ({ ast: { alts: [t], neg: [] } })))
  })
  it('no minimum in the `/…/` fallback', () => {
    expect(simple.parse('/gr/')).toEqual({ ast: { alts: [[re('gr')]], neg: [] } })
  })
})

describe('`simple`: `\^`, `\$`, `\\` escapes — literal `^`/`$`', () => {
  const at = (text: string, a: { start?: true; end?: true }): Matcher => ({ kind: 'sub', text, ...a })
  // [box text, the one matcher it parses to, that matcher written back by `matcherText`]
  it.each<[string, Matcher, string]>([
    ['\\^abc', sub('^abc'), '\\^abc'],
    ['abc\\$', sub('abc$'), 'abc\\$'],
    ['\\^abc\\$', sub('^abc$'), '\\^abc\\$'],
    ['ab\\^c', sub('ab^c'), 'ab^c'],
    ['ab\\$c', sub('ab$c'), 'ab$c'],
    ['abc\\\\', sub('abc\\'), 'abc\\\\'],
    ['a\\\\b', sub('a\\b'), 'a\\b'],
    ['a\\\\\\$', sub('a\\$'), 'a\\\\\\$'],
    ['\\^', sub('^'), '\\^'],
    ['\\$', sub('$'), '\\$'],
    ['"^abc"', sub('^abc'), '\\^abc'],
    ['"abc$"', sub('abc$'), 'abc\\$'],
    ['^abc', at('abc', { start: true }), '^abc'],
    ['abc$', at('abc', { end: true }), 'abc$'],
    ['^abc$', at('abc', { start: true, end: true }), '^abc$'],
    ['^abc\\$', at('abc$', { start: true }), '^abc\\$'],
    ['\\^abc$', at('^abc', { end: true }), '\\^abc$'],
    ['abc\\\\$', at('abc\\', { end: true }), 'abc\\\\$'],
    ['^^abc', at('^abc', { start: true }), '^\\^abc'],
    ['\\^ab*cd', glob('^ab', 'cd'), '\\^ab*cd'],
    ['ab*cd\\$', glob('ab', 'cd$'), 'ab*cd\\$'],
  ])('%s', (q, m, text) => {
    expect([simple.parse(q), matcherText(m), simple.parse(text)]).toEqual([{ ast: { alts: [[m]], neg: [] } }, text, { ast: { alts: [[m]], neg: [] } }])
  })
  it('any other `\\` is itself, as before (`\\x`, a trailing `\\`, `\\*` then a wildcard); quoted, a `\\` escapes nothing', () => {
    expect(['a\\bc', 'abc\\', 'a\\*bcd', '"abc\\$"', '\\"abc"'].map(q => simple.parse(q))).toEqual(
      [[sub('a\\bc')], [sub('abc\\')], [glob('a\\', 'bcd')], [sub('abc\\$')], [sub('\\abc')]].map(t => ({ ast: { alts: [t], neg: [] } })))
  })
  it('in an exclusion and among terms', () => {
    expect(simple.parse('usd\\$ -\\^tmp ckpt$')).toEqual({ ast: { alts: [[sub('usd$'), at('ckpt', { end: true })]], neg: [sub('^tmp')] } })
  })
  it('an escape counts as one character toward the minimum', () => {
    expect(['ab\\$ ckpt', 'a\\$ ckpt'].map(q => simple.parse(q))).toEqual<ParseResult[]>([
      { ast: { alts: [[sub('ab$'), sub('ckpt')]], neg: [] } },
      { error: 'type at least 3 characters (“a\\$”)', code: 'short-term' },
    ])
  })
})

describe('`regex`: parse table', () => {
  it.each<[string, ParseResult]>([
    ['ckpt.*final', { ast: { alts: [[re('ckpt.*final')]], neg: [] } }],
    ['  ^bk/tmp/ ', { ast: { alts: [[re('^bk/tmp/')]], neg: [] } }],
    ['a|b -x', { ast: { alts: [[re('a|b -x')]], neg: [] } }],
    ['/x/', { ast: { alts: [[re('/x/')]], neg: [] } }],
    ['', { ast: null }],
    [' ', { ast: null }],
    ['gr', { ast: { alts: [[re('gr')]], neg: [] } }],
    ['a(', { error: 'invalid regex: Invalid regular expression: /a(/i: Unterminated group', code: 'invalid-regex' }],
    ['*x', { error: 'invalid regex: Invalid regular expression: /*x/i: Nothing to repeat', code: 'invalid-regex' }],
  ])('%j', (q, want) => {
    expect(regex.parse(q)).toEqual(want)
  })
  it('is never planned: the view reads as before', () => {
    const ast = parseAst('ckpt', regex)!
    expect([planPositive(ast), planNegative(ast)]).toEqual([null, null])
  })
})

describe('the registry', () => {
  it('ids, default first', () => {
    expect([SYNTAXES.map(s => s.id), DEFAULT_SYNTAX.id]).toEqual([['simple', 'regex'], 'simple'])
  })
  it('`qs=` over the deployment default over `simple`; an unknown `qs=` is an error, an unknown default falls back', () => {
    expect([
      resolveSyntax(null).id,
      resolveSyntax(undefined, 'regex').id,
      resolveSyntax('simple', 'regex').id,
      resolveSyntax('regex').id,
      resolveSyntax('', 'nope').id,
    ]).toEqual(['simple', 'regex', 'simple', 'regex', 'simple'])
    expect(() => resolveSyntax('glob')).toThrowError("unknown query syntax 'glob' (want simple|regex)")
  })
  it('`parseAst` throws a typed parse error', () => {
    const codeOf = (q: string) => { try { parseAst(q); return null } catch (e) { return [(e as QueryError).code, (e as Error).message] } }
    expect([codeOf('/a(/'), codeOf('gr x'), codeOf('ttl')]).toEqual([
      ['invalid-regex', 'invalid regex: Invalid regular expression: /a(/i: Unterminated group'],
      ['short-term', 'type at least 3 characters (“gr”)'],
      null,
    ])
  })
  it('every help example parses in its own syntax', () => {
    const bad = SYNTAXES.flatMap(s => s.describe().forms.filter(f => !s.parse(f.example).ast).map(f => `${s.id}: ${f.example}`))
    expect(bad).toEqual([])
  })
})

describe('`queryParam`: what the API handlers read', () => {
  const qp = (qs: string, deflt?: string) => {
    const r = queryParam(new URLSearchParams(qs), deflt)
    return { ast: r.query?.ast ?? null, syntax: r.syntax }
  }
  it('`q=` in `qs=`, else the deployment default', () => {
    expect([qp('q=abc|def'), qp('q=abc|def&qs=regex'), qp('q=abc|def', 'regex'), qp('q=abc|def&qs=simple', 'regex'), qp('')]).toEqual([
      { ast: { alts: [[sub('abc')], [sub('def')]], neg: [] }, syntax: 'simple' },
      { ast: { alts: [[re('abc|def')]], neg: [] }, syntax: 'regex' },
      { ast: { alts: [[re('abc|def')]], neg: [] }, syntax: 'regex' },
      { ast: { alts: [[sub('abc')], [sub('def')]], neg: [] }, syntax: 'simple' },
      { ast: null, syntax: 'simple' },
    ])
  })
  it('throws on a parse error or an unknown syntax (the handlers’ 400)', () => {
    expect(() => qp('q=a(&qs=regex')).toThrowError('invalid regex: Invalid regular expression: /a(/i: Unterminated group')
    expect(() => qp('q=a&qs=nope')).toThrowError("unknown query syntax 'nope' (want simple|regex)")
  })
})
