import { describe, expect, it } from 'vitest'
import { Totals } from './view'

describe('Totals', () => {
  it('holds a node as numbers until asked for, then as the Agg it builds once', () => {
    const t = new Totals()
    t.addLite('a/b', 5, 1, true)
    t.addLite('a/b/c', 3, 1, false)
    t.grow('a/b', 2, 1)
    const before = { size: t.size, keys: [...t.keys()], b: t.bo('a/b'), has: [t.has('a/b'), t.has('a/x')], none: t.bo('a/x') }
    const a = t.get('a/b')!
    const built = [a.b, a.o, a.kind, a.ub, a.cb]
    t.grow('a/b', 1, 0)
    expect({
      before,
      built,
      same: t.get('a/b') === a,
      after: { size: t.size, keys: [...t.keys()], b: t.bo('a/b'), leaf: t.get('a/b/c')!.kind },
    }).toEqual({
      before: { size: 2, keys: ['a/b', 'a/b/c'], b: { b: 7, o: 2 }, has: [true, false], none: { b: 0, o: 0 } },
      built: [7, 2, 'dir', {}, {}],
      same: true,
      after: { size: 2, keys: ['a/b', 'a/b/c'], b: { b: 8, o: 2 }, leaf: null },
    })
  })

  it('refuses to iterate its values, which would skip the numbers-only nodes', () => {
    const t = new Totals()
    t.addLite('a', 1, 1, true)
    const err = (f: () => unknown) => { try { f(); return null } catch (e) { return (e as Error).message } }
    expect([err(() => [...t]), err(() => t.entries()), err(() => t.values()), err(() => t.forEach(() => {}))])
      .toEqual(Array(4).fill('Totals: iterate keys() and get/bo each'))
  })
})
