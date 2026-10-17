// A scan run's pipeline DAG (specs/pipelines-everywhere.md "Run reports"): the engine-neutral graph its head stored
// (`scan_run_dags`, reduced by the writer from its engine's own format), laid out in columns, and each node joined to
// the phases its tasks recorded (`scan_run_phases.task` = `<id> (<tag>)`; Nextflow: `$NF_PROCESS` / `$NF_TAG`).
import { phaseStatus, type ScanRunPhase } from './scanRunsModel'

/** The engine-neutral graph the run's writer stored (`dt_cloud.run_dag`): the steps, and the edges between them. A
 * node's `id` is what the run's phases name as their `task`'s process. */
export interface Dag { nodes: { id: string; label: string }[]; edges: [string, string][] }

/** The `scan_run_dags` row as the API answers it */
export interface RunDagRow { engine: string; dag: Dag }

/** Each node's column: its longest path from a source (a process with no inputs). */
export function dagLayers(dag: Dag): Map<string, number> {
  const ins = new Map(dag.nodes.map(({ id }) => [id, dag.edges.filter(([, t]) => t === id).map(([f]) => f)]))
  const layer = new Map<string, number>()
  const at = (n: string, stack: Set<string>): number => {
    if (layer.has(n)) return layer.get(n)!
    if (stack.has(n)) return 0  // a cycle: Nextflow's DAGs have none
    stack.add(n)
    const l = Math.max(-1, ...(ins.get(n) ?? []).map(f => at(f, stack))) + 1
    stack.delete(n)
    layer.set(n, l)
    return l
  }
  for (const { id } of dag.nodes) at(id, new Set())
  return layer
}

export type NodeStatus = ScanRunPhase['status'] | 'pending' | 'unrecorded'

/** One task of a process (a fan-out's member, or the process's only task) and the phases it recorded. */
export interface DagTask { task: string; tag: string; job_name: string | null; machine: string | null; phases: string[]; status: ScanRunPhase['status']; start: number | null; end: number | null }

export interface DagNode { id: string; label: string; layer: number; row: number; status: NodeStatus; tasks: DagTask[]; start: number | null; end: number | null }

/** The DAG node a phase ran in, from its `task` (`<id> (<tag>)`). */
export const processOf = (task: string): string => task.replace(/ \(.*\)$/, '')
const tagOf = (task: string): string => /\((.*)\)$/.exec(task)?.[1] ?? ''

function worst(ss: readonly ScanRunPhase['status'][]): ScanRunPhase['status'] {
  return ss.includes('failed') ? 'failed' : ss.includes('running') ? 'running' : 'done'
}

/** The DAG's nodes with their tasks' phases and a status: no phases yet is `pending` while the run runs, else
 * `unrecorded` (a task that recorded nothing, or a run before phases named their task). */
export function dagNodes(dag: Dag, phases: readonly ScanRunPhase[], running: boolean): DagNode[] {
  const layers = dagLayers(dag)
  const rows = new Map<number, number>()
  return dag.nodes.map(({ id, label }) => {
    const by = new Map<string, DagTask>()
    for (const p of [...phases].sort((a, b) => (a.started_ts ?? Infinity) - (b.started_ts ?? Infinity) || a.seq - b.seq)) {
      if (!p.task || processOf(p.task) !== id) continue
      const t = by.get(p.task) ?? { task: p.task, tag: tagOf(p.task), job_name: p.job_name ?? null, machine: p.machine ?? null, phases: [], status: 'done' as const, start: null, end: null }
      t.phases.push(p.phase)
      t.status = worst([t.status, phaseStatus(p)])
      if (p.started_ts != null) t.start = t.start == null ? p.started_ts : Math.min(t.start, p.started_ts)
      if (p.finished_ts != null) t.end = t.end == null ? p.finished_ts : Math.max(t.end, p.finished_ts)
      by.set(p.task, t)
    }
    const tasks = [...by.values()]
    const layer = layers.get(id) ?? 0
    const row = rows.get(layer) ?? 0
    rows.set(layer, row + 1)
    const starts = tasks.flatMap(t => t.start ?? []), ends = tasks.flatMap(t => t.end ?? [])
    return {
      id, label, layer, row, tasks,
      status: tasks.length ? worst(tasks.map(t => t.status)) : running ? 'pending' : 'unrecorded',
      start: starts.length ? Math.min(...starts) : null,
      end: ends.length && !tasks.some(t => t.status === 'running') ? Math.max(...ends) : null,
    }
  })
}
