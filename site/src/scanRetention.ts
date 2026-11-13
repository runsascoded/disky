/** Scan-level retention state over run-level history. Retries share one decision; maintenance is never a scan. */
import type { HistoryPlan } from '../functions/_lib/historyAdmin'
import { inSpan, type RunSpan } from './runChart'
import { isMaintenance, type RunSummary, type ScanRun } from './scanRunsModel'
import { isScanId } from './scanSlug'

export interface RetentionCandidate {
  scan: string
  protected?: boolean
  reasons?: string[]
  score?: number | null
  left?: { count: number } | null
  right?: { count: number } | null
}
export type RetentionPlan = HistoryPlan & { candidates?: RetentionCandidate[] }
export interface RetentionSource { scans: readonly string[]; pinned: readonly string[] }
export interface RetentionScan {
  scan: string
  available: boolean
  protected: boolean
  reasons: string[]
  proposal: 'retain' | 'remove' | null
  score: number | null
  left: number | null
  right: number | null
}

const scanRun = (run: ScanRun): boolean => !isMaintenance(run.kind) && run.kind === 'scan' && run.parent === null && isScanId(run.scan)

/** A reviewed plan's roster is authoritative, otherwise the current writer's source roster is. Successful loaded
 * runs are provisional until either arrives; their first/latest are conservatively protected. The backend always
 * revalidates the complete roster and operational pins before a build. */
export function retentionScans(
  runs: readonly RunSummary[],
  plan: RetentionPlan | null = null,
  sourceRoster: RetentionSource | null = null,
): Map<string, RetentionScan> {
  const source = runs.filter(({ run }) => scanRun(run))
  const published = new Set(source.filter(({ run }) => run.status === 'succeeded').map(({ run }) => run.scan))
  const available = [...new Set(plan ? plan.scans : sourceRoster ? sourceRoster.scans : published)].sort()
  const availableSet = new Set(available), retained = new Set(plan?.retained ?? [])
  const pins = new Set(plan ? [...(plan.pinned ?? []), ...(plan.required_pins ?? [])] : sourceRoster?.pinned ?? [])
  const candidates = new Map((plan?.candidates ?? []).map(c => [c.scan, c]))
  const steps = new Map((plan?.steps ?? []).map(s => [s.scan, s]))
  const scans = [...new Set([...source.map(({ run }) => run.scan), ...available])].sort()
  return new Map(scans.map(scan => {
    const candidate = candidates.get(scan), step = steps.get(scan), reasons = [...(candidate?.reasons ?? [])]
    if (availableSet.has(scan) && scan === available[0]) reasons.push('first scan')
    if (availableSet.has(scan) && scan === available.at(-1)) reasons.push('latest scan')
    if (pins.has(scan)) reasons.push('pinned')
    if (candidate?.protected && !reasons.length) reasons.push('protected by plan')
    const row: RetentionScan = {
      scan, available: availableSet.has(scan), protected: reasons.length > 0, reasons: [...new Set(reasons)],
      proposal: plan && availableSet.has(scan) ? retained.has(scan) ? 'retain' : 'remove' : null,
      score: step?.score ?? candidate?.score ?? null,
      left: step?.left.count ?? candidate?.left?.count ?? null,
      right: step?.right.count ?? candidate?.right?.count ?? null,
    }
    return [scan, row]
  }))
}

export function retentionSelectable(scan: RetentionScan | undefined): boolean {
  return !!scan?.available && !scan.protected
}

export function retentionRunSelectable(run: ScanRun, scans: ReadonlyMap<string, RetentionScan>): boolean {
  return scanRun(run) && retentionSelectable(scans.get(run.scan))
}

export function toggleRetentionScan(selected: readonly string[], scan: string, scans: ReadonlyMap<string, RetentionScan>): string[] {
  const ids = new Set(selected)
  if (retentionSelectable(scans.get(scan))) {
    if (ids.has(scan)) ids.delete(scan)
    else ids.add(scan)
  }
  return [...ids].sort()
}

/** Only an explicit brush action selects dates. A window itself continues to filter runs, as before. */
export function retentionSpanScans(runs: readonly RunSummary[], span: RunSpan | null, scans: ReadonlyMap<string, RetentionScan>): string[] {
  if (!span) return []
  return [...new Set(runs.filter(({ run }) => retentionRunSelectable(run, scans) && inSpan(span, run.started_ts)).map(({ run }) => run.scan))].sort()
}

export function retentionLabel(scan: RetentionScan | undefined, selected = false): string {
  if (!scan?.available) return 'Not published in the reviewed history'
  if (scan.protected) return `Keep · ${scan.reasons.join(', ')}`
  if (selected) return 'Selected for removal'
  return scan.proposal === 'remove' ? 'Proposed removal' : scan.proposal === 'retain' ? 'Retain' : 'Available'
}
