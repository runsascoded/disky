/** A cell's size text (`TreemapProps.formatCell`): one string, or candidates longest first — the renderer draws the
 *  first that fits the room its label leaves (e.g. a diff's `+37.2 T −20.4 T = +16.8 T`, else `+16.8 T`). */
export type CellText = string | readonly string[]

/** The fewest px a cell's name keeps beside an inline size before a longer size candidate gives way. */
export const NAME_MIN_PX = 48

export const candidates = (t: CellText): readonly string[] => (typeof t === 'string' ? [t] : t)

/** The first of `cs` whose width (`measure`) is at most `room` px; past every one, the last (the shortest: the
 *  renderer clips it as it clips any size). */
export function fitCandidate(cs: readonly string[], room: number, measure: (s: string) => number): string {
  for (const c of cs) if (measure(c) <= room) return c
  return cs[cs.length - 1] ?? ''
}

/** The px an inline size may take in a title bar `boxW` px wide (padding `pad` per side, `gap` before the size): all
 *  of it but the name's, kept down to `NAME_MIN_PX` (or the whole name when shorter). */
export const inlineRoom = (boxW: number, pad: number, gap: number, nameW: number): number =>
  boxW - 2 * pad - gap - Math.min(nameW, NAME_MIN_PX)
