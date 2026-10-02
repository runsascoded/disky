import { describe, expect, it } from 'vitest'
import { makeSimple, parseAst as parseWith } from './querySyntax'

// Planner cases include short terms (`gr`): `simple` without its minimum.
const parseAst = (q: string) => parseWith(q, makeSimple({ minTerm: 1 }))
import { and, type Formula, or, planNegative, planPositive, triCode, trigrams } from './searchQuery'

/** A string's formula, spelled out: AND of its trigrams. */
const all = (s: string): Formula => and(trigrams(s))
const formulas = (q: string): Formula[] | null => planPositive(parseAst(q)!)?.branches.map(b => b.formula) ?? null
/** Which of `names` a plan's branches accept (the exact name test). */
const accepts = (q: string, names: string[]): string[] => {
  const p = planPositive(parseAst(q)!)!
  return names.filter(n => p.branches.some(b => b.test(n)))
}

describe('trigrams', () => {
  it('distinct, lowercased, ASCII-only, ascending codes', () => {
    expect(trigrams('TTLttl')).toEqual([triCode('ltt'), triCode('tlt'), triCode('ttl')])
    expect(trigrams('ab')).toEqual([])
    // `é` is not ASCII: only `caf` survives of `caf`, `afé`, `fés`.
    expect(trigrams('Cafés')).toEqual([triCode('caf')])
    // The Kelvin sign lowercases to `k` in JS, as in DuckDB.
    expect(trigrams('\u212aey')).toEqual([triCode('key')])
  })
})

describe('positive terms (AND = the union of the terms’ candidates)', () => {
  it('a slash-free term: the root’s last segment contains it', () => {
    expect(formulas('ttl')).toEqual([triCode('ttl')])
    expect(formulas(' TTL=14d ')).toEqual([all('ttl=14d')])
    expect(accepts('TTL', ['ttl=7d', 'iris-TTL', 'tl', 'x'])).toEqual(['ttl=7d', 'iris-TTL'])
  })
  it('alternatives and AND terms are all branches; blanks dropped', () => {
    expect(formulas('ttl | ckpt ||')).toEqual([triCode('ttl'), all('ckpt')])
    expect(formulas('ckpt final|tmp')).toEqual([all('ckpt'), all('final'), all('tmp')])
    expect(accepts('ckpt final', ['run-final', 'ckpt', 'x'])).toEqual(['run-final', 'ckpt'])
  })
  it('a term with `/`: the last segment starts with its last piece', () => {
    expect(formulas('run-a/ckpt')).toEqual([all('ckpt')])
    expect(accepts('run-a/CKPT', ['ckpt', 'ckpt-final.pt', 'x-ckpt'])).toEqual(['ckpt', 'ckpt-final.pt'])
  })
  it('`*`: literal pieces as trigram conjunctions, the segment checked exactly', () => {
    expect(formulas('ckpt*final')).toEqual([and([all('ckpt'), all('final')])])
    expect(accepts('ckpt*final', ['ckpt-run-b-final', 'CKPTFINAL', 'final-ckpt', 'ckpt'])).toEqual(['ckpt-run-b-final', 'CKPTFINAL'])
    expect(formulas('*.safetensors')).toEqual([all('.safetensors')])
    // With `/`: the part after the last `/`, anchored at the segment's start.
    expect(formulas('tmp/*/ck*pt')).toEqual([true])
    expect(accepts('tmp/*/ck*pt', ['ckpt', 'ck-x-pt', 'x-ckpt'])).toEqual(['ckpt', 'ck-x-pt'])
    expect(formulas('tmp/run*ckpt')).toEqual([and([all('run'), all('ckpt')])])
    expect(accepts('tmp/run*ckpt', ['run-a-ckpt', 'a-run-ckpt'])).toEqual(['run-a-ckpt'])
  })
  it('a term ending in `/` constrains no segment: not served', () => {
    expect(planPositive(parseAst('ckpt/')!)).toBeNull()
    expect(planPositive(parseAst('ttl|ckpt/')!)).toBeNull()
  })
  it('a term under 3 characters has no trigram: unconstrained (the names scan)', () => {
    expect(formulas('gr')).toEqual([true])
    expect(formulas('ttl|gr')).toEqual([triCode('ttl'), true])
  })
  it('no positive part to search: only negatives, or the regex fallback', () => {
    expect([planPositive(parseAst('-x')!), planPositive(parseAst('a|-x')!), planPositive(parseAst('/ttl/')!)]).toEqual([null, null, null])
  })
})

describe('negative terms', () => {
  it('the excluded paths’ candidates: one branch per NOT term', () => {
    // `run*b`: `b` is under 3 characters, so `run` alone narrows it.
    const p = planNegative(parseAst('ckpt -tmp -run*b')!)!
    expect(p.branches.map(b => b.formula)).toEqual([all('tmp'), all('run')])
    expect(['tmp-1', 'run-ab', 'run', 'ckpt'].filter(n => p.branches.some(b => b.test(n)))).toEqual(['tmp-1', 'run-ab'])
    expect(planNegative(parseAst('ckpt')!)).toBeNull()
  })
})

describe('formulas', () => {
  it('OR absorbs an alternative that needs a superset of another’s trigrams', () => {
    expect(or([all('ckpt'), all('ckpts')])).toEqual(all('ckpt'))
    expect(or([all('ckpt'), all('final')])).toEqual({ or: [all('ckpt'), all('final')] })
  })
})
