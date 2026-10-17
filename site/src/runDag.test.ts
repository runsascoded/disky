// The DAG view's model (src/runDag.ts): layout and the join of nodes to phases, over the engine-neutral graph.
import { describe, expect, it } from 'vitest'
import { type Dag, dagLayers, dagNodes } from './runDag'
import type { ScanRunPhase } from './scanRunsModel'

// What `dt_cloud.run_dag.from_nextflow` stores for job/nf/scan.nf over cw's profile (cloud/tests/test_run_dag.py)
const node = (id: string) => ({ id, label: id })
const DAG: Dag = {
  nodes: ['list_source', 'index', 'static_names', 'publish_r2', 'interval_store', 'index_rows', 'post'].map(node),
  edges: [
    ['list_source', 'index'],
    ['index', 'index_rows'], ['index', 'publish_r2'], ['index', 'static_names'],
    ['static_names', 'post'],
    ['publish_r2', 'interval_store'],
    ['interval_store', 'post'],
    ['index_rows', 'post'],
  ],
}

const ph = (phase: string, seq: number, task: string, start: number, end: number | null, o: Partial<ScanRunPhase> = {}): ScanRunPhase => ({
  run_id: 'r', phase, seq, started_ts: start, finished_ts: end, status: end == null ? 'running' : 'done', note: null, task, ...o,
})

describe('dagLayers', () => {
  it('lays each node out at its longest path from a source', () => {
    expect(Object.fromEntries(dagLayers(DAG))).toEqual({
      list_source: 0, index: 1, static_names: 2, publish_r2: 2, interval_store: 3, index_rows: 2, post: 4,
    })
  })
})

describe('dagNodes', () => {
  const phases = [
    ph('list b1', 0, 'list_source (s3://b1)', 100, 400, { job_name: 'nf-1', machine: 'n2-standard-16' }),
    ph('import b1', 1, 'list_source (s3://b1)', 400, 500, { job_name: 'nf-1', machine: 'n2-standard-16' }),
    ph('bucket-check', 2, 'list_source (s3://b2)', 110, 115, { job_name: 'nf-2' }),
    ph('list b2', 3, 'list_source (s3://b2)', 115, 200, { job_name: 'nf-2' }),
    ph('layer-2', 4, 'index', 600, 620),
    ph('index-write', 5, 'index', 620, 900),
    ph('static-names', 6, 'static_names', 950, null),
    ph('legacy', 7, '', 0, 10),
  ]
  it('joins each node to its tasks\' phases', () => {
    const nodes = dagNodes(DAG, phases, true)
    expect(nodes.map(n => [n.id, n.layer, n.row, n.status, n.start, n.end, n.tasks.map(t => [t.tag, t.job_name, t.phases, t.status])])).toEqual([
      ['list_source', 0, 0, 'done', 100, 500, [['s3://b1', 'nf-1', ['list b1', 'import b1'], 'done'], ['s3://b2', 'nf-2', ['bucket-check', 'list b2'], 'done']]],
      ['index', 1, 0, 'done', 600, 900, [['', null, ['layer-2', 'index-write'], 'done']]],
      ['static_names', 2, 0, 'running', 950, null, [['', null, ['static-names'], 'running']]],
      ['publish_r2', 2, 1, 'pending', null, null, []],
      ['interval_store', 3, 0, 'pending', null, null, []],
      ['index_rows', 2, 2, 'pending', null, null, []],
      ['post', 4, 0, 'pending', null, null, []],
    ])
  })
  it('calls a finished run\'s phaseless node unrecorded, and a failed task fails its node', () => {
    const failed = [...phases.slice(0, 6), ph('static-names', 6, 'static_names', 950, 990, { status: 'failed' })]
    expect(dagNodes(DAG, failed, false).map(n => [n.id, n.status])).toEqual([
      ['list_source', 'done'], ['index', 'done'], ['static_names', 'failed'], ['publish_r2', 'unrecorded'],
      ['interval_store', 'unrecorded'], ['index_rows', 'unrecorded'], ['post', 'unrecorded'],
    ])
  })
})
