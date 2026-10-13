import { describe, expect, it } from 'vitest'
import { matchRoots } from './filter'
import { parseQuery } from './pathQuery'
import { regex } from './querySyntax'
import { planPositive } from './searchQuery'

const PATHS = [
  'bk/tmp/ttl=14d/run-a/ckpt',
  'bk/tmp/ttl=7d',
  'bk/x/ckpt/run/final',
  'bk/x/ckpt-final',
  'bk/x/ckpt-run-b-final',
  'bk/x/ckpt/final.pt',
  'bk/models/Llama/model-1.safetensors',
  'bk/models/tiny.safetensors',
  'bk/notes/a b.txt',
  'bk/notes/-draft',
  'bk/notes/a|b',
]
/** The paths a query's predicate accepts, in `PATHS` order. */
const hits = (q: string): string[] => PATHS.filter(parseQuery(q)!)

describe('parseQuery: the predicate on the full path', () => {
  it.each([
    // substring anywhere, case-insensitive
    ['ckpt', ['bk/tmp/ttl=14d/run-a/ckpt', 'bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
    ['LLAMA', ['bk/models/Llama/model-1.safetensors']],
    // OR
    ['ttl=7d|tiny', ['bk/tmp/ttl=7d', 'bk/models/tiny.safetensors']],
    // AND, in any segments
    ['ckpt final', ['bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
    // OR binds looser than AND
    ['ckpt run|tiny', ['bk/tmp/ttl=14d/run-a/ckpt', 'bk/x/ckpt/run/final', 'bk/x/ckpt-run-b-final', 'bk/models/tiny.safetensors']],
    // NOT
    ['ckpt -run', ['bk/x/ckpt-final', 'bk/x/ckpt/final.pt']],
    ['ckpt -run|tiny', ['bk/x/ckpt-final', 'bk/x/ckpt/final.pt', 'bk/models/tiny.safetensors']],
    // only negatives: everything except
    ['-x -tmp -notes', ['bk/models/Llama/model-1.safetensors', 'bk/models/tiny.safetensors']],
    // `*` stays inside one segment
    ['ckpt*final', ['bk/x/ckpt-final', 'bk/x/ckpt-run-b-final']],
    ['ckpt/*/final', ['bk/x/ckpt/run/final']],
    ['*.safetensors', ['bk/models/Llama/model-1.safetensors', 'bk/models/tiny.safetensors']],
    // a term with `/`: a substring of the full path
    ['x/ckpt', ['bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
    // quoted: literal spaces, a leading `-`, `|`
    ['"a b"', ['bk/notes/a b.txt']],
    ['"-draft"', ['bk/notes/-draft']],
    ['"a|b"', ['bk/notes/a|b']],
    ['notes -"a b"', ['bk/notes/-draft', 'bk/notes/a|b']],
    // anchored: a segment starts with, ends with, or is the term (never a match inside a segment)
    ['^ckpt', ['bk/tmp/ttl=14d/run-a/ckpt', 'bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
    ['^final', ['bk/x/ckpt/run/final', 'bk/x/ckpt/final.pt']],
    ['final$', ['bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final']],
    ['^ckpt$', ['bk/tmp/ttl=14d/run-a/ckpt', 'bk/x/ckpt/run/final', 'bk/x/ckpt/final.pt']],
    ['.safetensors$', ['bk/models/Llama/model-1.safetensors', 'bk/models/tiny.safetensors']],
    ['^llama$', ['bk/models/Llama/model-1.safetensors']],
    ['^bk', PATHS],
    ['^model*.safetensors$', ['bk/models/Llama/model-1.safetensors']],
    ['^tmp/ttl', ['bk/tmp/ttl=14d/run-a/ckpt', 'bk/tmp/ttl=7d']],
    ['ckpt -^run', ['bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
    ['"^ckpt"', []],
    // the regex fallback: full-path semantics, spanning segments
    ['/ckpt.*final/', ['bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
  ])('%s', (q, want) => {
    expect(hits(q)).toEqual(want)
  })
  it('carries its AST and halves: `pos` is monotone along a path, `neg` null without negatives', () => {
    const p = parseQuery('ckpt -run')!
    expect([p.ast, p.pos!('bk/x/ckpt/run'), p.neg!('bk/x/ckpt/run'), p('bk/x/ckpt/run'), p('bk/x/ckpt')]).toEqual([
      { alts: [[{ kind: 'sub', text: 'ckpt' }]], neg: [{ kind: 'sub', text: 'run' }] }, true, true, false, true,
    ])
    expect(parseQuery('ckpt')!.neg).toBeNull()
  })
})

describe('escaped `^`/`$` end to end: a name with a literal `$` or `^`', () => {
  // A listing whose names hold the characters themselves, beside names the anchors would match.
  const ROWS = ['fx', 'fx/usd', 'fx/usd$', 'fx/usd$/q1', 'fx/usd$x', 'fx/^tmp', 'fx/^tmp/a', 'fx/tmp', 'fx/eur/usd']
  /** The outermost matches under the store root, and which names the search index's plan accepts as a match root. */
  const map = (q: string) => {
    const pred = parseQuery(q)!
    const plan = planPositive(pred.ast!)!
    return { roots: matchRoots(ROWS, pred, ''), names: ['usd', 'usd$', 'usd$x', '^tmp', 'tmp'].filter(n => plan.branches.some(b => b.test(n))) }
  }
  it.each([
    ['usd\\$', { roots: ['fx/usd$', 'fx/usd$x'], names: ['usd$', 'usd$x'] }],
    ['"usd$"', { roots: ['fx/usd$', 'fx/usd$x'], names: ['usd$', 'usd$x'] }],
    ['usd$', { roots: ['fx/eur/usd', 'fx/usd'], names: ['usd'] }],
    ['^usd\\$$', { roots: ['fx/usd$'], names: ['usd$'] }],
    ['\\^tmp', { roots: ['fx/^tmp'], names: ['^tmp'] }],
    ['^tmp', { roots: ['fx/tmp'], names: ['tmp'] }],
    ['^\\^tmp', { roots: ['fx/^tmp'], names: ['^tmp'] }],
  ])('%s', (q, want) => {
    expect(map(q)).toEqual(want)
  })
})

describe('parseQuery in the `regex` syntax: the whole query is a full-path regex', () => {
  const rx = (q: string): string[] => PATHS.filter(parseQuery(q, regex)!)
  it.each([
    ['ckpt.*final', ['bk/x/ckpt/run/final', 'bk/x/ckpt-final', 'bk/x/ckpt-run-b-final', 'bk/x/ckpt/final.pt']],
    ['ckpt[^/]*final', ['bk/x/ckpt-final', 'bk/x/ckpt-run-b-final']],
    ['^bk/models/', ['bk/models/Llama/model-1.safetensors', 'bk/models/tiny.safetensors']],
    ['LLAMA', ['bk/models/Llama/model-1.safetensors']],
    // `|` and `-` are regex characters here, not OR / NOT terms
    ['ttl=7d$|tiny', ['bk/tmp/ttl=7d', 'bk/models/tiny.safetensors']],
    ['/-draft', ['bk/notes/-draft']],
    ['a b', ['bk/notes/a b.txt']],
  ])('%s', (q, want) => {
    expect(rx(q)).toEqual(want)
  })
})
