/** The static name index's hex-run rule (specs/static-hex-runs.md; `cloud/src/dt_cloud/hex_runs.py`, which this
 *  ports exactly): interiors of long hex ids are not indexed.
 *
 *  On a lowercase string `l` (a name or a whole path: `/` is not hex, so a run never crosses a segment), a **hex run**
 *  is a maximal substring of `[0-9a-f]` of at least `min` characters, `l[a..b]`. With `before` = the hex characters
 *  just before position `p` and `after` = those from `p` on, an occurrence of length `m` at `p` is **dropped** iff
 *  `before ≥ 1 ∧ after ≥ 1 ∧ before + after ≥ min ∧ (after > tail ∨ m ≤ after)`: it starts strictly inside a run and
 *  either before the run's last `tail` positions, or in them without extending past the run's end. A rule of `null`
 *  is the full index (a generation built before the rule): plain `includes` / `indexOf`.
 *
 *  Positions are UTF-16 units here and code points in Python; the rule only counts ASCII hex characters next to `p`
 *  and compares them with `m`, so both agree (`hexRuns.test.ts` runs the shared table). */
export interface HexRule { min: number; tail: number }

const isHex = (c: number): boolean => (c >= 48 && c <= 57) || (c >= 97 && c <= 102)

/** The hex characters just before `p` and from `p` on. */
function around(l: string, p: number): [number, number] {
  let before = 0, after = 0
  while (p - before - 1 >= 0 && isHex(l.charCodeAt(p - before - 1))) before++
  while (p + after < l.length && isHex(l.charCodeAt(p + after))) after++
  return [before, after]
}

/** Whether `p` is strictly inside a run. */
const inside = (before: number, after: number, rule: HexRule): boolean => before >= 1 && after >= 1 && before + after >= rule.min

/** Whether an occurrence of length `m` at `p` of `l` is dropped by the rule. */
export function dropped(l: string, p: number, m: number, rule: HexRule | null): boolean {
  if (!rule) return false
  const [before, after] = around(l, p)
  return inside(before, after, rule) && (after > rule.tail || m <= after)
}

/** Whether `p` starts no suffix row (inside a run, before its tail). */
export function opaque(l: string, p: number, rule: HexRule | null): boolean {
  if (!rule) return false
  const [before, after] = around(l, p)
  return inside(before, after, rule) && after > rule.tail
}

/** The starts of `l`'s suffix rows: every position with ≥ 3 units left that is not opaque. */
export function keptPositions(l: string, rule: HexRule | null): number[] {
  const out: number[] = []
  for (let p = 0; p < l.length - 2; p++) if (!opaque(l, p, rule)) out.push(p)
  return out
}

/** The first position where `q` occurs in `l` under the rule, or −1. */
export function firstOccurrence(q: string, l: string, rule: HexRule | null, from = 0): number {
  let p = l.indexOf(q, from)
  while (p >= 0 && dropped(l, p, q.length, rule)) p = l.indexOf(q, p + 1)
  return p
}

/** Whether literal `q` occurs in `l` (both lowercase) under the rule. */
export const occurs = (q: string, l: string, rule: HexRule | null): boolean => firstOccurrence(q, l, rule) >= 0

/** Whether some occurrence of `q` can be dropped (so a response says so): hex throughout, or a leading hex prefix
 *  longer than the tail. Any other literal matches exactly as in the full index. */
export function hexAffected(q: string, rule: HexRule | null): boolean {
  if (!rule || !q) return false
  const l = q.toLowerCase()
  let lead = 0
  while (lead < l.length && isHex(l.charCodeAt(lead))) lead++
  return lead === l.length || lead > rule.tail
}

/** A recorded rule (`catalog/meta.json` `hex_runs`), checked; absent = the full index. */
export function ruleOf(x: unknown): HexRule | null {
  if (x == null) return null
  const r = x as Partial<HexRule>
  if (typeof r !== 'object' || Object.keys(r).sort().join() !== 'min,tail' || !Number.isSafeInteger(r.min) || !Number.isSafeInteger(r.tail) || r.min! < 2 || r.tail! < 0 || r.tail! >= r.min!) {
    throw new Error(`static names: bad hex_runs ${JSON.stringify(x)}`)
  }
  return { min: r.min!, tail: r.tail! }
}

export const sameRule = (a: HexRule | null, b: HexRule | null): boolean => a === b || (!!a && !!b && a.min === b.min && a.tail === b.tail)

/** An anchored literal's mode (`^q` start, `q$` end, `^q$` exact; null: a plain contains literal). */
export type AnchorMode = 'start' | 'end' | 'exact'

/** Whether a lowercase segment holds `q` in `mode` under the rule: anchored search's per-segment test. An occurrence
 *  at a segment's start has no hex digit before it, so it is never dropped: `^q` and `^q$` are `startsWith` and `===`
 *  under any rule. `q$`'s one candidate occurrence ends the segment and is tested by `dropped` like any other: e.g. a
 *  proper suffix of a trailing 16+ hex-digit run is dropped (it can't extend past the run), and so is a `q` with more
 *  than `tail` leading hex digits starting inside a run. So only `hexAffected` literals can lose `q$` matches. */
export function segmentOccurs(seg: string, q: string, mode: AnchorMode | null, rule: HexRule | null): boolean {
  switch (mode) {
    case 'start': return seg.startsWith(q)
    case 'exact': return seg === q
    case 'end': return seg.endsWith(q) && !dropped(seg, seg.length - q.length, q.length, rule)
    default: return occurs(q, seg, rule)
  }
}
