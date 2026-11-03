import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { countText, KIND_DEFS, NOTIFY, type Notify } from '../functions/_lib/activityShape'

// `/admin`'s "Activity" table (specs/done/activity-feed.md), and the helpers `/admin/activity` shares with it.

export async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...init })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error((j as { error?: string }).error ?? `HTTP ${r.status}`)
  return j as T
}

/** A burst's counts, in report order. */
export const countsText = (counts: Record<string, number>): string =>
  KIND_DEFS.filter(d => (counts[d.kind] ?? 0) > 0).map(d => countText(d.kind, counts[d.kind])).join(' · ')

/** An event line (written for Slack / Discord): its `code` spans as `<code>`. */
export const Line = ({ text }: { text: string }) => <>{text.split(/(`[^`]*`)/).map((p, i) => p.startsWith('`') && p.endsWith('`') && p.length > 1 ? <code key={i}>{p.slice(1, -1)}</code> : p)}</>

interface KindState { kind: string; source: 'server' | 'client'; covers: string; default: { log: boolean; notify: Notify }; log: boolean; notify: Notify }
interface KindsBody { kinds: KindState[]; sinks: { slack: boolean; discord: boolean } }

const NOTIFY_LABEL: Record<Notify, string> = { off: 'off', count: 'count', line: 'each one' }

/** `/admin`'s "Activity" section: per kind, whether it is recorded and how the admin channel reports it. */
export function ActivityKinds() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['activity', 'kinds'], queryFn: () => getJson<KindsBody>('/api/activity/kinds') })
  const set = useMutation<KindsBody, Error, { kind: string; log: boolean; notify: Notify }>({
    mutationFn: ({ kind, ...conf }) => getJson<KindsBody>(`/api/activity/kinds/${kind}`, {
      method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(conf),
    }),
    onSuccess: b => qc.setQueryData(['activity', 'kinds'], b),
  })
  const sinks = q.data?.sinks
  return (
    <section className="activity-kinds">
      <h2>Activity</h2>
      <p>
        What people do in the app, by kind: recorded for <Link to="/admin/activity">/admin/activity</Link> and reported
        to the admin channel, one thread per person per burst of activity. <em>Count</em> adds the kind to the thread's
        summary; <em>each one</em> also lists every event in a reply. Page loads, URL changes and clicks are recorded
        only while the session log (below) is on.
        {sinks && <> Posting to: {[sinks.slack && 'Slack', sinks.discord && 'Discord'].filter(Boolean).join(' and ') || <b>nowhere (no Slack channel or Discord webhook configured)</b>}.</>}
      </p>
      {q.error && <p className="err">{q.error.message}</p>}
      {set.error && <p className="err">{set.error.message}</p>}
      {q.data && (
        <div className="table-scroll">
          <table className="activity-kinds-table">
            <thead><tr><th>kind</th><th>covers</th><th>record</th><th>admin channel</th></tr></thead>
            <tbody>
              {q.data.kinds.map(k => {
                const changed = k.log !== k.default.log || k.notify !== k.default.notify
                return (
                  <tr key={k.kind} className={k.log ? '' : 'off'}>
                    <td><code>{k.kind}</code>{changed && <span className="dim" title={`default: ${k.default.log ? 'recorded' : 'not recorded'}, ${NOTIFY_LABEL[k.default.notify]}`}> *</span>}</td>
                    <td>{k.covers}{k.source === 'client' && <span className="dim"> (session log)</span>}</td>
                    <td><input type="checkbox" aria-label={`record ${k.kind}`} checked={k.log} disabled={set.isPending} onChange={e => set.mutate({ kind: k.kind, log: e.target.checked, notify: k.notify })} /></td>
                    <td>
                      <select aria-label={`report ${k.kind}`} value={k.notify} disabled={set.isPending || !k.log} onChange={e => set.mutate({ kind: k.kind, log: k.log, notify: e.target.value as Notify })}>
                        {NOTIFY.map(n => <option key={n} value={n}>{NOTIFY_LABEL[n]}</option>)}
                      </select>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
