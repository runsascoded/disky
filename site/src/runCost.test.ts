import { describe, expect, it } from 'vitest'
import { costStages, jobStage, type RunCostJob, type RunCostReport, runCostKey, upkeepOf } from './runCost'

const job = (o: Partial<RunCostJob>): RunCostJob => ({
  job: 'j', uid: 'u', region: 'us-east1', association: 'scan_run_label', component: 'scan', stage: null, step: null, upkeep: null,
  state: 'SUCCEEDED', machine: 'n2-highmem-16', provisioning_model: 'SPOT', tasks: 1, observed_vm_seconds: 3600,
  estimated_compute_usd: 1, warnings: [], ...o,
})

const report = (jobs: RunCostJob[]): RunCostReport => ({
  scan: '2026-11-26T1203', scan_run: 'r', root_uid: 'r', as_of: '', estimated_compute_usd: 0, components: {}, upkeep_usd: {},
  ongoing_jobs: [], unpriced_jobs: [], legacy_inference: false, exclusions: [], jobs,
})

describe('runCost', () => {
  it('names each job by its stage: the head, a task by its process, a submitted job by its component and kind', () => {
    expect([
      job({ association: 'root', job: 'cw-usage-snapshot-20261126-12' }),
      job({ step: 'list_source' }),
      job({ step: 'index_rows' }),
      job({ step: 'static_names' }),
      job({ component: 'drill', stage: 'drill' }),
      job({ component: 'static-names', stage: 'append' }),
      job({ component: 'interval-store', stage: 'ranges' }),
      job({ component: 'listing' }),
      job({ step: 'mystery' }),
      job({ component: 'other-thing' }),
    ].map(jobStage)).toEqual([
      { stage: 'head', label: 'head' },
      { stage: 'list', label: 'list-source' },
      { stage: 'index', label: 'index-rows' },
      { stage: 'static names', label: 'static-names' },
      { stage: 'static names', label: 'drill' },
      { stage: 'static names', label: 'append' },
      { stage: 'interval store', label: 'ranges' },
      { stage: 'list', label: 'listing' },
      { stage: 'other', label: 'mystery' },
      { stage: 'other', label: 'other-thing' },
    ])
  })

  it('groups the run’s jobs by stage in pipeline order, sums each, and lists upkeep apart', () => {
    const r = report([
      job({ component: 'anchors', stage: 'anchors', job: 'sn-anchors', estimated_compute_usd: 0.92 }),
      job({ association: 'root', job: 'head', estimated_compute_usd: 0.2, observed_vm_seconds: 7200 }),
      job({ component: 'drill', stage: 'drill', job: 'sn-drill', estimated_compute_usd: 1.42 }),
      job({ component: 'static-names', stage: 'r2', job: 'sn-r2', estimated_compute_usd: null }),
      job({ component: 'static-names', stage: 'merge', upkeep: 'carry', job: 'sn-merge', estimated_compute_usd: 2.62 }),
    ])
    const { stages, total, upkeep } = costStages(r)
    expect(total).toBe(0.2 + (0.92 + 1.42))
    expect(stages.map(s => [s.stage, s.usd, s.vmHours, s.jobs.map(j => j.label)])).toEqual([
      ['head', 0.2, 2, ['head']],
      ['static names', 0.92 + 1.42, 3, ['anchors', 'drill', 'r2']],
    ])
    expect(upkeep && [upkeep.stage, upkeep.usd, upkeep.jobs.map(j => j.label)]).toEqual(['store upkeep', 2.62, ['carry (merge)']])
    expect(costStages(report([job({ association: 'root' })])).upkeep).toBeNull()
  })

  it('puts every job in exactly one group: the stages and upkeep together are the report’s total', () => {
    const jobs = [
      job({ association: 'root', estimated_compute_usd: 0.1979 }), job({ step: 'list_source', estimated_compute_usd: 0.5 }),
      job({ component: 'anchors', stage: 'anchors', estimated_compute_usd: 0.9181 }), job({ component: 'mystery', estimated_compute_usd: 0.25 }),
      job({ component: 'static-names', stage: 'merge', estimated_compute_usd: 2.621 }), job({ component: 'static-names', stage: 'r2', estimated_compute_usd: null }),
    ]
    const { stages, total, upkeep } = costStages(report(jobs))
    expect([stages.reduce((n, s) => n + s.jobs.length, 0) + (upkeep?.jobs.length ?? 0), stages.map(s => s.stage)]).toEqual([6, ['head', 'list', 'static names', 'other']])
    expect((total ?? 0) + (upkeep?.usd ?? 0)).toBeCloseTo(0.1979 + 0.5 + 0.9181 + 0.25 + 2.621, 10)
  })

  it('reads a store’s merge or compaction job from before the upkeep label as upkeep', () => {
    expect([
      job({ component: 'static-names', stage: 'merge', upkeep: undefined }),
      job({ component: 'interval-store', stage: 'compact' }),
      job({ component: 'static-names', stage: 'compact-r2' }),
      job({ component: 'static-names', stage: 'drill' }),
      job({ component: 'scan', stage: 'merge' }),
    ].map(upkeepOf)).toEqual(['carry', 'fold', 'fold', null, null])
  })

  it('keys a report by scan and run, refusing anything else', () => {
    expect([
      runCostKey('2026-11-26T1203', 'cw-usage-snapshot-f6a3dacd-c6f7-442900'),
      runCostKey('2026-11-26', 'r1'),
      runCostKey('2026-11-26T1203', '../x'),
      runCostKey('2026-11-26T1203', '.hidden'),
      runCostKey('../2026', 'r'),
    ]).toEqual([
      'snapshots/cost/runs/2026-11-26T1203/cw-usage-snapshot-f6a3dacd-c6f7-442900.json',
      'snapshots/cost/runs/2026-11-26/r1.json',
      null, null, null,
    ])
  })
})
