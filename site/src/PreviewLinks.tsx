import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

interface TokenRow {
  id: number
  token: string
  kind: string
  page: string
  minted_by: string
  minted_ts: number
  exp_day: number
  revoked_by: string | null
  revoked_ts: number | null
}

// Day 0 of packed expiries (`functions/_lib/og/sign.ts` EPOCH): 2026-01-01.
const EPOCH_MS = Date.UTC(2026, 0, 1)
const fmtDay = (day: number) => new Date(EPOCH_MS + day * 86400_000).toISOString().slice(0, 10)
const fmtTs = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

/** `/admin`'s list of full-card share links (specs/done/dogi.md): who minted which
 * view's token, until when; revoking one turns that view's previews back to
 * the anonymous card (and every mint of the same token with it). */
export function PreviewLinks() {
  const qc = useQueryClient()
  const q = useQuery<{ tokens: TokenRow[] }>({
    queryKey: ['og-tokens'],
    queryFn: async () => {
      const r = await fetch('/api/og/tokens', { credentials: 'include' })
      if (!r.ok) throw new Error(`${r.status}`)
      return r.json()
    },
    retry: false,
  })
  const revoke = useMutation({
    mutationFn: async (token: string) => {
      const r = await fetch(`/api/og/tokens/${encodeURIComponent(token)}`, { method: 'DELETE', credentials: 'include' })
      if (!r.ok) throw new Error(`${r.status}`)
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['og-tokens'] }),
  })
  if (q.error) return null
  const rows = q.data?.tokens ?? []
  return (
    <section className="preview-links">
      <h2>Preview links</h2>
      <p>
        Links minted with “Copy link with preview”: their unfurl card shows the full view (labels, sizes, owners) for that exact page and params.
        Plain links unfurl as unlabelled shapes. Revoke to send a view back to the anonymous card.
      </p>
      <div className="table-scroll">
        <table className="grants">
          <thead><tr><th>page</th><th>minted by</th><th>when</th><th>until</th><th /></tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id} className={r.revoked_ts ? 'revoked' : undefined}>
                <td><Link to={r.page}>{r.page}</Link></td>
                <td>{r.minted_by}</td>
                <td>{fmtTs(r.minted_ts)}</td>
                <td>{fmtDay(r.exp_day)}</td>
                <td>
                  {r.revoked_ts
                    ? <span className="revoked-label">revoked {fmtTs(r.revoked_ts)}</span>
                    : <button type="button" className="icon-btn danger" onClick={() => revoke.mutate(r.token)} disabled={revoke.isPending}>revoke</button>}
                </td>
              </tr>
            ))}
            {!rows.length && !q.isPending && <tr><td colSpan={5}><em>none minted yet</em></td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
