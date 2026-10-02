import type { ColorMode } from './types'

// Which page-bar controls a store + scan can honour. Each axis is gated on
// what backs it — not on "has attribution" (which, on a store without it,
// hid the whole bar, scan picker aside): colors need their data (read a read
// range, owner attribution), the class axis needs class bytes, the owner
// filter attribution, and the path filter nothing at all.
export interface BarFacts {
  /** Attribution + claims (`Store.owners`) — the owner color and filter. */
  owners: boolean
  /** The scan carries attribution (`meta.users`). */
  hasAttr: boolean
  /** Storage classes exist here (`Store.prices`). */
  classes: boolean
  /** The scan carries a read range (`meta.access`). */
  readRange: boolean
}

export interface BarControls {
  /** Color-by options, in menu order. */
  color: ColorMode[]
  /** The "shade by storage class" secondary axis. */
  shade: boolean
  /** The storage-class multi-select. */
  classes: boolean
  /** The owner pool / user filter. */
  ownerFilter: boolean
  /** The path filter box (`text · a|b · a b · -x · *`). */
  pathFilter: true
}

export function barControls(f: BarFacts): BarControls {
  const color: ColorMode[] = []
  if (f.readRange) color.push('read')
  if (f.owners && f.hasAttr) color.push('user')
  color.push('date', 'tree')
  return {
    color,
    shade: f.classes,
    classes: f.classes,
    ownerFilter: f.owners && f.hasAttr,
    pathFilter: true,
  }
}
