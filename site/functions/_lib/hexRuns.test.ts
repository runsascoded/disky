import { describe, expect, it } from 'vitest'
import { fixture } from './testStore.js'
import { firstOccurrence, hexAffected, type HexRule, keptPositions, occurs, ruleOf } from './hexRuns.js'

// The shared table (`cloud/tests/test_hex_runs.py` writes it from the Python rule): per rule, every string's
// suffix starts, every (literal, string)'s first occurrence, and which literals are hex-affected.
type Cases = Record<string, { kept: Record<string, number[]>; first: [string, string, number][]; affected: Record<string, boolean> }>
const RULES: Record<string, HexRule | null> = { '16,8': { min: 16, tail: 8 }, '16,0': { min: 16, tail: 0 }, off: null }

const load = async (): Promise<Cases> => {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string, enc: string): string }
  return JSON.parse(fs.readFileSync(fixture('hex-runs/cases.json'), 'utf8'))
}

describe('hex runs: the TS rule equals the Python one on the shared table', async () => {
  const cases = await load()
  it('covers every rule', () => expect(Object.keys(cases).sort()).toEqual(Object.keys(RULES).sort()))
  for (const [name, rule] of Object.entries(RULES)) {
    const c = cases[name]
    it(`${name}: suffix starts`, () => {
      expect(Object.fromEntries(Object.keys(c.kept).map(s => [s, keptPositions(s, rule)]))).toEqual(c.kept)
    })
    it(`${name}: first occurrences and occurs`, () => {
      expect(c.first.map(([q, s]) => [q, s, firstOccurrence(q, s, rule)])).toEqual(c.first)
      expect(c.first.map(([q, s]) => occurs(q, s, rule))).toEqual(c.first.map(([, , at]) => at >= 0))
    })
    it(`${name}: hex-affected literals`, () => {
      expect(Object.fromEntries(Object.keys(c.affected).map(q => [q, hexAffected(q, rule)]))).toEqual(c.affected)
    })
  }
})

describe('hex runs: spec examples', () => {
  const R = { min: 16, tail: 8 }
  const glued = 'obj_3f9a2b7c1d0e4f5a6b7c8d9e0f1a2b3cdata.json'
  it('a glued word is found, a hash interior is not, the hash prefix is', () => {
    expect(['data', '2b3c', '7c1d', '3f9a', '.json'].map(q => firstOccurrence(q, glued, R))).toEqual([36, -1, -1, 4, 40])
  })
  it('only hex-throughout literals, or > 8 leading hex digits, are affected', () => {
    expect(['cafe', 'bad', '1234', '2024', 'data', '.json', 'deadbeefx', 'deadbeef0x'].map(q => hexAffected(q, R))).toEqual([true, true, true, true, false, false, false, true])
  })
  it('reads a recorded rule, refusing a bad one', () => {
    expect([ruleOf(undefined), ruleOf({ min: 16, tail: 8 })]).toEqual([null, R])
    expect(() => ruleOf({ min: 16 })).toThrow('static names: bad hex_runs {"min":16}')
    expect(() => ruleOf({ min: 8, tail: 8 })).toThrow('static names: bad hex_runs {"min":8,"tail":8}')
  })
})
