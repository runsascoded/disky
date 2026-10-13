// The scan-run pieces /scans and /health share (specs/scan-runs-ui.md, specs/health-page.md): the run list's fetch
// (one React Query cache entry for both pages), the status chip, the phase bar and its colors.
import { useQuery } from '@tanstack/react-query'
import { Tooltip } from './Tooltip'
import { fmtDur as fmtDurM } from './runs'
import type { PhaseSpan, RunSummary, ScanRun } from './scanRunsModel'

export interface Links { project: string | null; meta: string | null }
export type ListResponse = Links & { configured: boolean; runs: RunSummary[] }

export async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: 'include' })
  const text = await r.text()
  let data: unknown = null
  try { data = JSON.parse(text) } catch { data = null }
  if (!r.ok) throw new Error((data as { error?: string } | null)?.error ?? `${r.status} ${r.statusText}`)
  return data as T
}

export const useScanRuns = () => useQuery<ListResponse, Error>({ queryKey: ['scan-runs'], queryFn: () => getJson('/api/scan-runs'), refetchInterval: 60_000, staleTime: 15_000 })

/** `fmtDur`, with seconds under a minute (a 3 s publish isn't `0m`). */
export const fmtDur = (s: number): string => (s < 60 ? `${Math.round(s)}s` : fmtDurM(s))

export const utc = (ts: number | null): string => (ts == null ? '—' : new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' '))
const PALETTE = ['--s1', '--s2', '--s4', '--s7', '--s5', '--s6', '--s3', '--s8']
/** A stable color per phase name: its position in the usual phase order. */
export const colorer = (order: string[]) => (phase: string) => `var(${PALETTE[Math.max(0, order.indexOf(phase)) % PALETTE.length]})`

export function Status({ run, failedAt }: { run: ScanRun; failedAt?: string | null }) {
  const tag = <span className={`sr-status ${run.status}`}>{run.status === 'nop' ? 'no-op' : run.status}</span>
  if (run.status !== 'failed') return tag
  return <Tooltip content={<>{failedAt && <div>Failed {failedAt.startsWith('after') ? failedAt : `in ${failedAt}`}</div>}{run.error && <div className="sr-err">{run.error}</div>}</>}>{tag}</Tooltip>
}

/** A run's phases as one bar (the main lane; overlapped phases are in its tip). */
export function PhaseBar({ spans, secs, color, width = 160 }: { spans: PhaseSpan[]; secs: number | null; color: (p: string) => string; width?: number }) {
  if (!secs) return <span className="dim">—</span>
  const x = (t: number) => (t / secs) * width
  const main = spans.filter(s => s.lane === 0)
  return (
    <Tooltip content={spans.length ? <table className="sr-tip"><tbody>{spans.map(s => <tr key={s.phase}><td><i className="sw" style={{ background: color(s.phase) }} />{s.phase}{s.lane ? ' (beside)' : ''}</td><td className="num">{fmtDur(s.end - s.start)}</td></tr>)}</tbody></table> : 'No phase markers recorded for this run'}>
      <svg className="sr-bar" width={width} height={10} viewBox={`0 0 ${width} 10`} role="img" aria-label={`${fmtDur(secs)} in ${main.length} phases`}>
        <rect className="sr-bar-bg" x={0} y={0} width={width} height={10} rx={2} />
        {main.map(s => <rect key={s.phase} x={x(s.start)} y={0} width={Math.max(1, x(s.end) - x(s.start))} height={10} style={{ fill: color(s.phase) }} />)}
      </svg>
    </Tooltip>
  )
}
