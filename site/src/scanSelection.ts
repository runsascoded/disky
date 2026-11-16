import type { ScanSel } from './scanSlug'

/** A pick (no `before`) retains endpoint modes: a duration start shifts with the end. A brush (`before`: the scan its
 *  start edge names) becomes the range: its start is pinned there, whatever the mode was, so a later scan can't move it
 *  to whichever scan is nearest "end − duration". An end on the latest scan stays latest. Explicit toggles change modes. */
export function pickScanSelection(
  current: ScanSel | undefined,
  after: string | undefined,
  latest: string | null,
  span: number | undefined,
  before?: string,
): ScanSel | undefined {
  const d = after && (current?.d !== undefined || after !== latest) ? after : undefined
  const from = before ?? current?.from
  const out = { ...(d ? { d } : {}), ...(from ? { from } : span ? { span } : {}) }
  return Object.keys(out).length ? out : undefined
}
