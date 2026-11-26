/**
 * A scan run's compute report (`dt-cloud scan-run cost -O`, the `run-cost` step: `snapshots/cost/runs/<scan>/<run>.json`)
 * and its jobs grouped by stage for /scans (specs/scan-cost-by-stage.md). Pure: the run page and the route share it.
 *
 * Terms: a run has stages (list → index → stores → publish → post); a stage has jobs (Batch jobs: what's billed); store
 * upkeep (carries, folds) serves every scan it merges and is listed apart from the run's total.
 */

/** One job's row in the report (`scan_cost.job_estimate`). */
export interface RunCostJob {
  job: string
  uid: string
  region: string
  association: string
  component: string
  /** The job's `stage` label: its kind within its stage (static names' `append`, `drill`, …). */
  stage: string | null
  /** A Nextflow task's process (`list_source`, `index`, `static_names`, …). Absent in reports from before 2026-11-26. */
  step?: string | null
  /** `carry` / `fold`: store upkeep. Absent in reports from before 2026-11-26. */
  upkeep?: string | null
  state: string
  machine: string | null
  provisioning_model: string | null
  tasks: number
  observed_vm_seconds: number
  estimated_compute_usd: number | null
  /** What the billing export says it cost (cost + credits; spot prices, disks): null until its lines arrive (about a day
   *  late), absent in a report written without the export (`run-cost` with no `billing`). */
  billed_usd?: number | null
  warnings: string[]
}

export interface RunCostReport {
  scan: string
  scan_run: string
  root_uid: string
  as_of: string
  estimated_compute_usd: number
  components: Record<string, number>
  upkeep_usd?: Record<string, number>
  /** The billing export's totals (`scan_cost.with_billed`): its own jobs', its upkeep's, and how many jobs have a line. */
  billed_usd?: number | null
  billed_upkeep_usd?: number | null
  billed_jobs?: number
  ongoing_jobs: string[]
  unpriced_jobs: string[]
  legacy_inference: boolean
  exclusions: string[]
  jobs: RunCostJob[]
}

/** A stage's jobs and their summed estimate (null when none is priced). */
export interface CostStage {
  stage: string
  usd: number | null
  /** Its jobs' billed lines summed (null: none has one yet). */
  billed: number | null
  vmHours: number
  jobs: { label: string; job: RunCostJob }[]
}

/** The pipeline's stages, in order, and what each Nextflow process and job component belongs to. */
export const STAGES = ['head', 'list', 'index', 'static names', 'interval store', 'publish', 'post', 'other'] as const

const STEP_STAGE: Record<string, (typeof STAGES)[number]> = {
  list_source: 'list', list: 'list', bucket_check: 'list',
  index: 'index', index_part: 'index', index_rows: 'index', index_sync: 'post',
  static_names: 'static names', interval_store: 'interval store',
  publish_r2: 'publish', post: 'post', over_time: 'post',
}

const COMPONENT_STAGE: Record<string, (typeof STAGES)[number]> = {
  listing: 'list', 'static-names': 'static names', drill: 'static names', anchors: 'static names',
  'interval-store': 'interval store', 'vm-pack': 'post',
}

/** Which stage a (non-upkeep) job belongs to, and how it's named within it. */
export function jobStage(j: RunCostJob): { stage: (typeof STAGES)[number]; label: string } {
  if (j.association === 'root') return { stage: 'head', label: 'head' }
  if (j.component === 'scan' && j.step) return { stage: STEP_STAGE[j.step] ?? 'other', label: j.step.replace(/_/g, '-') }
  const stage = COMPONENT_STAGE[j.component] ?? 'other'
  return { stage, label: j.stage ?? j.component }
}

const sumOrNull = (xs: (number | null)[]): number | null => {
  const priced = xs.filter((x): x is number => x != null)
  return priced.length ? priced.reduce((a, b) => a + b, 0) : null
}

/** A job's upkeep kind: its `upkeep` label, else (a report from before the label) a store's merge or compaction job. */
export function upkeepOf(j: RunCostJob): string | null {
  if (j.upkeep) return j.upkeep
  const store = j.component === 'static-names' || j.component === 'interval-store'
  if (store && j.stage === 'merge') return 'carry'
  if (store && (j.stage === 'compact' || j.stage === 'compact-r2')) return 'fold'
  return null
}

/** The run's own jobs by stage (in `STAGES` order, empty stages left out), their total (a report's own `estimated_compute_usd`
 *  counts a carry from before the upkeep label), and its store upkeep apart. */
export function costStages(r: RunCostReport): { stages: CostStage[]; total: number | null; billed: number | null; upkeep: CostStage | null } {
  const by = new Map<string, CostStage['jobs']>()
  const upkeep: CostStage['jobs'] = []
  for (const job of r.jobs) {
    const kind = upkeepOf(job)
    if (kind) {
      upkeep.push({ label: `${kind} (${job.stage ?? job.component})`, job })
      continue
    }
    const { stage, label } = jobStage(job)
    by.set(stage, [...(by.get(stage) ?? []), { label, job }])
  }
  const stage = (name: string, jobs: CostStage['jobs']): CostStage => ({
    stage: name, jobs: [...jobs].sort((a, b) => a.label.localeCompare(b.label) || a.job.job.localeCompare(b.job.job)),
    usd: sumOrNull(jobs.map(j => j.job.estimated_compute_usd)),
    billed: sumOrNull(jobs.map(j => j.job.billed_usd ?? null)),
    vmHours: jobs.reduce((a, j) => a + j.job.observed_vm_seconds / 3600, 0),
  })
  const stages = STAGES.filter(s => by.has(s)).map(s => stage(s, by.get(s)!))
  return {
    stages, total: sumOrNull(stages.map(s => s.usd)), billed: sumOrNull(stages.map(s => s.billed)),
    upkeep: upkeep.length ? stage('store upkeep', upkeep) : null,
  }
}

/** A report's key under the served store, or null for a scan or run id that isn't one (never a path outside it). */
export const RUN_COST_PREFIX = 'snapshots/cost/runs/'
export function runCostKey(scan: string, runId: string): string | null {
  return /^\d{4}-\d{2}-\d{2}(T\d{4})?$/.test(scan) && /^[A-Za-z0-9._-]{1,128}$/.test(runId) && !runId.startsWith('.')
    ? `${RUN_COST_PREFIX}${scan}/${runId}.json`
    : null
}
