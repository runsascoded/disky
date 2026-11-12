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
 * owners and class bytes summed, newest write and last read the
 * max). Prefixes without stats (gone by this scan, or not loaded) are left
 * out; children sort heaviest first.
 */
export function stagedTree(prefixes: string[], stats: Record<string, PrefixStat>, rootName: string): TreeNode {
  type Mut = { n: string; c: Map<string, Mut>; leaf?: PrefixStat }
  const root: Mut = { n: rootName, c: new Map() }
  for (const p of prefixes) {
    const s = stats[p]
    if (!s || s.bytes <= 0) continue
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
      return { name: m.n, kind: 'dir', bytes: s.bytes, objects: s.objects, ...(s.mtime != null ? { mtime: s.mtime } : {}), ...(s.last_read != null ? { last_read: s.last_read } : {}), ...(s.owners?.length ? { owners: s.owners } : {}), ...(s.class_bytes ? { class_bytes: s.class_bytes } : {}) }
    }
    const children = [...m.c.values()].map(build).sort((x, y) => y.bytes - x.bytes)
    const bytes = children.reduce((t, k) => t + k.bytes, 0)
    const objects = children.reduce((t, k) => t + k.objects, 0)
    const ms = children.filter(k => k.mtime != null).map(k => k.mtime!)
    const as = children.filter(k => k.last_read != null).map(k => k.last_read!)
    const us = new Map<string, number>()
    for (const k of children) for (const [u, ub] of k.owners ?? []) us.set(u, (us.get(u) ?? 0) + ub)
    const cb = new Map<string, number>()
    for (const k of children) for (const [cl, v] of Object.entries(k.class_bytes ?? {})) cb.set(cl, (cb.get(cl) ?? 0) + v)
    return {
      name: m.n, kind: 'dir', bytes, objects,
      ...(ms.length ? { mtime: Math.max(...ms) } : {}),
      ...(as.length ? { last_read: Math.max(...as) } : {}),
      ...(us.size ? { owners: [...us].sort((x, y) => y[1] - x[1]) } : {}),
      ...(cb.size ? { class_bytes: Object.fromEntries([...cb].sort((x, y) => y[1] - x[1])) } : {}),
      children,
    }
  }
  return build(root)
}
