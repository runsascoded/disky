// The storage-class strip under the map: the drilled node's class mix (`classMix`) as one stacked bar, largest
// class first, each segment's share of the bytes and its list-price $/mo — pure, so the bar and its labels are
// tested apart from the page.
import { CLASS_NAMES, CLASS_PRICE_US } from './types'

export interface ClassSeg {
  /** The class id (`1` Standard … `4` Archive; an unknown id keeps its own). */
  c: string
  name: string
  bytes: number
  /** Share of the mix's bytes, 0–1. */
  frac: number
  /** List price, US regions, $/mo. */
  usd: number
}

/** The mix's non-empty classes as bar segments, largest first (ties by class id), and their total $/mo. */
export function classSegments(mix: Record<string, number>): { segs: ClassSeg[]; usd: number } {
  const rows = Object.entries(mix).filter(([, b]) => b > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
  const total = rows.reduce((s, [, b]) => s + b, 0)
  const segs = rows.map(([c, b]) => ({ c, name: CLASS_NAMES[c] ?? c, bytes: b, frac: b / total, usd: (b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02) }))
  return { segs, usd: segs.reduce((s, x) => s + x.usd, 0) }
}

/** A segment's share as a label: `<1%` under one percent, else whole percent. */
export const fracLabel = (f: number): string => (f > 0 && f < 0.01 ? '<1%' : `${Math.round(f * 100)}%`)
