import { elapsed, viewBuckets, viewState, type RunView } from './runs'

export interface RunTableOptions { user: string; mode: string; state: string; query: string; sort: string; ascending: boolean }
export interface RunMetrics { planned?: number; deleted?: number; rate?: number; bytes?: number }

const { ceil, floor, max, min } = Math

/** The page the table opens on with no page asked for: the open run's (`target`: its index among the listed runs, -1
 *  when none is open or it isn't listed), else the first. */
export const autoRunsPage = (target: number, size: number): number => (target >= 0 && Number.isFinite(size) ? floor(target / size) : 0)

/** The runs table's page: the one asked for (`?rp=`, 0-based; null: none), else `autoRunsPage`; clamped to the list. */
export const runsPage = (asked: number | null, target: number, size: number, total: number): number =>
  min(max(asked ?? autoRunsPage(target, size), 0), max(1, ceil(total / size)) - 1)

/** `page`'s rows among `total`, half-open. "All" (`size` Infinity) is one page holding every row. */
export const runsWindow = (page: number, size: number, total: number): [number, number] =>
  Number.isFinite(size) ? [min(total, page * size), min(total, (page + 1) * size)] : [0, total]

/** What `?rp=` holds for `page`: nothing when the table opens there anyway, so a link to an open run (`?run=`) pages
 *  to it, and one whose reader paged away from it says where to. */
export const askedRunsPage = (page: number, target: number, size: number): number | null => (page === autoRunsPage(target, size) ? null : page)

export function selectRuns(views: readonly RunView[], options: RunTableOptions, metrics: ReadonlyMap<string, RunMetrics>, now: number): RunView[] {
  const actor = (v: RunView) => v.run?.actor ?? v.job?.by ?? ''
  const mode = (v: RunView) => v.run?.mode ?? v.job?.mode ?? 'dry'
  const value = (v: RunView): string | number => {
    const r = v.run, m = metrics.get(v.key)
    switch (options.sort) {
      case 'by': return actor(v)
      case 'mode': return mode(v)
      case 'state': return viewState(v)
      case 'planned': return m?.planned ?? -1
      case 'deleted': return m?.deleted ?? r?.deleted_objects ?? -1
      case 'bytes': return m?.bytes ?? r?.deleted_bytes ?? -1
      case 'rate': return m?.rate ?? -1
      case 'elapsed': return elapsed(v, now) ?? -1
      case 'gone': return r?.skipped_gone ?? -1
      case 'overwritten': return r?.skipped_overwritten ?? -1
      case 'drift': return (r?.drift_dirs ?? 0) + (r?.ledger_drift_dirs ?? 0)
      case 'undo': return r?.undo_deadline ?? -1
      case 'buckets': return viewBuckets(v).join(', ')
      default: return r?.started_ts ?? (v.job?.created ? Date.parse(v.job.created) / 1000 : 0)
    }
  }
  return views.filter(v => (!options.user || actor(v) === options.user)
    && (!options.mode || mode(v) === options.mode)
    && (!options.state || viewState(v) === options.state)
    && (!options.query || [v.key, v.job?.job_id, actor(v), mode(v), viewState(v), ...viewBuckets(v)].join(' ').toLowerCase().includes(options.query.toLowerCase())))
    .sort((a, b) => {
      const av = value(a), bv = value(b)
      const order = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))
      return (options.ascending ? order : -order) || a.key.localeCompare(b.key)
    })
}
