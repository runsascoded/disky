// A set of prefixes as one treemap tree (a trie on bucket + path segments,
// the prefixes its leaves) — `/staged`'s map of what's staged. Pure, so the
// page's numbers are tested here.
import type { PrefixStat } from './prefixes'
import type { TreeNode } from './types'

/** `gs://bucket/a/b/` → `['bucket', 'a', 'b']`. */
export const prefixSegs = (prefix: string): string[] =>
  prefix.replace(/^[a-z0-9]+:\/\//, '').replace(/\/+$/, '').split('/')

/**
 * `prefixes` as one tree under `rootName`: interior nodes are the path
 * segments between them, re-aggregated from the leaves (bytes, objects,
 * owners and class bytes summed, created day byte-weighted, last read the
 * max). Prefixes without stats (gone by this scan, or not loaded) are left
 * out; children sort heaviest first.
 */
export function stagedTree(prefixes: string[], stats: Record<string, PrefixStat>, rootName: string): TreeNode {
  type Mut = { n: string; c: Map<string, Mut>; leaf?: PrefixStat }
  const root: Mut = { n: rootName, c: new Map() }
  for (const p of prefixes) {
    const s = stats[p]
    if (!s || s.b <= 0) continue
    let at = root
    for (const seg of prefixSegs(p)) {
      let next = at.c.get(seg)
      if (!next) at.c.set(seg, (next = { n: seg, c: new Map() }))
      at = next
    }
    at.leaf = s
  }
  const build = (m: Mut): TreeNode => {
    if (m.leaf) {
      const s = m.leaf
      return { n: m.n, k: 'dir', b: s.b, o: s.o, ...(s.d != null ? { d: s.d } : {}), ...(s.a != null ? { a: s.a } : {}), ...(s.us?.length ? { us: s.us } : {}), ...(s.cb ? { cb: s.cb } : {}) }
    }
    const c = [...m.c.values()].map(build).sort((x, y) => y.b - x.b)
    const b = c.reduce((t, k) => t + k.b, 0)
    const o = c.reduce((t, k) => t + k.o, 0)
    const dw = c.filter(k => k.d != null)
    const dB = dw.reduce((t, k) => t + k.b, 0)
    const as = c.filter(k => k.a != null).map(k => k.a!)
    const us = new Map<string, number>()
    for (const k of c) for (const [u, ub] of k.us ?? []) us.set(u, (us.get(u) ?? 0) + ub)
    const cb = new Map<string, number>()
    for (const k of c) for (const [cl, v] of Object.entries(k.cb ?? {})) cb.set(cl, (cb.get(cl) ?? 0) + v)
    return {
      n: m.n, k: 'dir', b, o,
      ...(dB > 0 ? { d: Math.round(dw.reduce((t, k) => t + k.d! * k.b, 0) / dB) } : {}),
      ...(as.length ? { a: Math.max(...as) } : {}),
      ...(us.size ? { us: [...us].sort((x, y) => y[1] - x[1]) } : {}),
      ...(cb.size ? { cb: Object.fromEntries([...cb].sort((x, y) => y[1] - x[1])) } : {}),
      c,
    }
  }
  return build(root)
}
