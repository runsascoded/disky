import { useInfiniteQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Tooltip } from './Tooltip'
import type { TaskLogPage } from '../functions/_lib/taskLogs'

export function RunTaskLogs({ job, region, live, explorer }: { job: string; region: string; live: boolean; explorer: string }) {
  const [chronological, setChronological] = useState(true)
  const [follow, setFollow] = useState(true)
  const scroll = useRef<HTMLDivElement>(null)
  const section = useRef<HTMLElement>(null)
  const anchored = useRef(false)
  const logs = useInfiniteQuery({
    queryKey: ['batch-task-logs', job, region],
    initialPageParam: null as { cursor: string; until: string } | null,
    queryFn: async ({ pageParam, signal }): Promise<TaskLogPage> => {
      const p = new URLSearchParams({ job, region })
      if (pageParam) { p.set('cursor', pageParam.cursor); p.set('until', pageParam.until) }
      const r = await fetch(`/api/sweep/logs?${p}`, { credentials: 'include', signal })
      const data = await r.json() as TaskLogPage & { error?: string }
      if (!r.ok) throw new Error(data.error ?? `Task logs: HTTP ${r.status}`)
      return data
    },
    getNextPageParam: (last, pages) => last.nextPageToken && pages.length < 10 ? { cursor: last.nextPageToken, until: last.until } : undefined,
    refetchInterval: live ? 10_000 : false,
    staleTime: 10_000,
    retry: 1,
  })
  const entries = logs.data?.pages.flatMap(p => p.entries) ?? []
  const seen = new Set<string>()
  const unique = entries.filter(e => { const key = `${e.timestamp}:${e.id}`; if (seen.has(key)) return false; seen.add(key); return true })
  const displayed = chronological ? [...unique].reverse() : unique
  const jumpLatest = () => { const el = scroll.current; if (el) el.scrollTop = chronological ? el.scrollHeight : 0 }
  useEffect(() => { if (live && follow) jumpLatest() }, [logs.dataUpdatedAt, live, follow, chronological])
  useEffect(() => {
    if (logs.isSuccess && !anchored.current && window.location.hash === '#task-logs') {
      anchored.current = true
      section.current?.scrollIntoView({ block: 'start' })
    }
  }, [logs.isSuccess])
  return <section className="run-task-logs" id="task-logs" ref={section}>
    <div className="log-controls"><h3>Batch task logs</h3><span className="dim">{live ? 'refreshes every 10s' : 'finished job'}</span><button type="button" onClick={() => setChronological(v => !v)}>{chronological ? 'chronological' : 'newest first'}</button><button type="button" onClick={jumpLatest}>latest entry</button>{live && <label><input type="checkbox" checked={follow} onChange={e => setFollow(e.target.checked)} /> follow latest</label>}<button type="button" disabled={logs.isFetching} onClick={() => void logs.refetch()}>refresh</button><a href={explorer} target="_blank" rel="noreferrer">Logs Explorer ↗</a></div>
    {logs.error && <p className="err">{logs.error.message}</p>}
    {logs.isPending ? <p>Loading task logs…</p> : !logs.error && unique.length === 0 ? <p className="dim">No task logs found in this page. {logs.hasNextPage ? 'Continue searching older entries below.' : 'They may not have arrived yet.'}</p> : null}
    {unique.length > 0 && <div className="task-log-scroll" ref={scroll} tabIndex={0} aria-label="Batch task log entries" onScroll={e => { const el = e.currentTarget; if (follow && (chronological ? el.scrollHeight - el.clientHeight - el.scrollTop > 32 : el.scrollTop > 32)) setFollow(false) }}><table><thead><tr><th>time (UTC)</th><th>severity</th><th>message</th></tr></thead><tbody>{displayed.map(e => <tr key={`${e.timestamp}:${e.id}`}><td className="log-time"><Tooltip content={e.timestamp}><time dateTime={e.timestamp}>{e.timestamp.replace('T', ' ').replace(/\.\d+Z$/, 'Z')}</time></Tooltip></td><td className="log-severity">{e.severity}</td><td><pre>{e.message}{e.truncated && '\n[Entry truncated; open Logs Explorer for the full payload.]'}</pre></td></tr>)}</tbody></table></div>}
    <p className="log-pagination"><span className="dim">{unique.length} entries shown</span>{logs.hasNextPage && <button type="button" disabled={logs.isFetching} onClick={() => { setFollow(false); void logs.fetchNextPage() }}>{logs.isFetchingNextPage ? 'loading…' : 'older entries'}</button>}{logs.data?.pages.length === 10 && <span className="dim">Panel limit: 500 entries. Use Logs Explorer for more.</span>}</p>
  </section>
}
