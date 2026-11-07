import type { ScanSel } from './scanSlug'

/** Pick/brush updates retain endpoint modes; explicit toggles change them. */
export function pickScanSelection(
  current: ScanSel | undefined,
  after: string | undefined,
  latest: string | null,
  span: number | undefined,
  before?: string,
): ScanSel | undefined {
  const d = after && (current?.d !== undefined || after !== latest) ? after : undefined
  const from = current?.from !== undefined && before ? before : current?.from
  const out = { ...(d ? { d } : {}), ...(from ? { from } : span ? { span } : {}) }
  return Object.keys(out).length ? out : undefined
}
