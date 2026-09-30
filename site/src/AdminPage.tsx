import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { DEFAULT_STORE } from './stores'
import { useDocTitle } from './title'

// Share-link console (staff-only; the backend enforces the `admin` scope on
// every /api/auth/grants route — this page just renders the 403 politely).
// Mint a link, copy it exactly once (the raw token is never shown again), and
// revoke it to kill every session it ever minted, instantly.

// Row-action icons (feather-style, stroke = currentColor so they theme + pick up
// the button's hover color). rotate = re-key; revoke = kill (slashed circle).
const RotateIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <polyline points="23 4 23 10 17 10" />
    <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
  </svg>
)
const RevokeIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="10" />
    <line x1="5.6" y1="5.6" x2="18.4" y2="18.4" />
  </svg>
)

/** The identity a link logs its holder in as (`grants.subject_json`). */
interface Subject {
  name?: string
  email?: string
  avatar?: string
}

interface Grant {
  id: string
  name: string | null
  note: string | null
  email: string | null
  subject: Subject | null
  scopes: string[]
  maxRedeems: number | null
  redeems: number
  expiresAt: number | null
  createdAt: number
  createdBy: string
  revokedAt: number | null
  lastUsedAt: number | null
}

const fmtTs = (ts: number | null): string => (ts ? new Date(ts * 1000).toLocaleString() : '—')

const linkFor = (token: string): string => `${window.location.origin}/?key=${token}`

/** Who a link is for: the person (`subject.name`, then their email), else the
 * grant's admin `name` — the fallback for CLI/agent-token grants. */
const holderName = (g: Grant): string | null => g.subject?.name || g.subject?.email || g.name || null

// Persist the in-progress mint form so a reload / redeploy doesn't wipe a draft.
// Per-tab (sessionStorage), cleared once the link is minted.
const DRAFT_KEY = 'admin-mint-draft'
interface Draft {
  memo: string
  name: string
  email: string
  avatar: string
  days: string
  readOnly: boolean
}
const loadDraft = (): Partial<Draft> => {
  try {
    return JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? '{}') as Partial<Draft>
  } catch {
    return {}
  }
}

export function AdminPage() {
  useDocTitle('Admin')
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const showRevoked = searchParams.get('revoked') === '1'
  const toggleRevoked = (on: boolean) =>
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (on) next.set('revoked', '1')
      else next.delete('revoked')
      return next
    }, { replace: true })
  const [draft] = useState(loadDraft)
  const [memo, setMemo] = useState(draft.memo ?? '')
  const [name, setName] = useState(draft.name ?? '')
  const [email, setEmail] = useState(draft.email ?? '')
  const [avatar, setAvatar] = useState(draft.avatar ?? '')
  const [days, setDays] = useState(draft.days ?? '30')
  const [readOnly, setReadOnly] = useState(draft.readOnly ?? true)
  const [minted, setMinted] = useState<{ label: string; url: string } | null>(null)

  useEffect(() => {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ memo, name, email, avatar, days, readOnly }))
    } catch {
      // sessionStorage can throw (private mode / disabled) — a lost draft is cosmetic.
    }
  }, [memo, name, email, avatar, days, readOnly])

  const grantsQ = useQuery<{ grants: Grant[] }, Error>({
    queryKey: ['auth', 'grants'],
    retry: false,
    queryFn: async () => {
      const r = await fetch('/api/auth/grants', { credentials: 'include' })
      if (r.status === 401 || r.status === 403) throw new Error('staff only')
      if (!r.ok) throw new Error(`grants: ${r.status}`)
      return r.json()
    },
  })

  const mint = useMutation({
    mutationFn: async () => {
      const expiresInS = days.trim() ? Number(days) * 86400 : null
      // memo → `note` (the link's admin-side label); name/email/avatar →
      // `subject_json`, the identity the link logs its holder in *as*
      // (`subjectName`, since the body's `name` is the grant's own label).
      const r = await fetch('/api/auth/grants', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          note: memo.trim(),
          subjectName: name.trim() || null,
          email: email.trim() || null,
          avatar: avatar.trim() || null,
          scopes: [readOnly ? `${DEFAULT_STORE.key}:read` : DEFAULT_STORE.key],
          expiresInS,
        }),
      })
      if (!r.ok) throw new Error(`create failed: ${r.status}`)
      return r.json() as Promise<{ grant: Grant; token: string }>
    },
    onSuccess: ({ grant, token }) => {
      setMinted({ label: holderName(grant) ?? grant.note ?? 'unnamed', url: linkFor(token) })
      setMemo('')
      setName('')
      setEmail('')
      setAvatar('')
      setReadOnly(true)
      try {
        sessionStorage.removeItem(DRAFT_KEY)
      } catch {
        // ignore
      }
      void qc.invalidateQueries({ queryKey: ['auth', 'grants'] })
    },
  })

  const revoke = useMutation({
    mutationFn: async (id: string) => {
      const r = await fetch(`/api/auth/grants/${id}/revoke`, { method: 'POST', credentials: 'include' })
      if (!r.ok) throw new Error(`revoke failed: ${r.status}`)
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['auth', 'grants'] }),
  })

  // Rotate = re-key a (leaked) link: new token, same grant — subject/scopes/
  // expiry intact, the old ?key= stops resolving. Re-key-only by default; the
  // API also takes { endSessions: true } to boot sessions already inside.
  const rotate = useMutation({
    mutationFn: async (id: string) => {
      const r = await fetch(`/api/auth/grants/${id}/rotate`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ endSessions: false }),
      })
      if (!r.ok) throw new Error(`rotate failed: ${r.status}`)
      return r.json() as Promise<{ id: string; token: string }>
    },
    onSuccess: ({ token }) => {
      setMinted({ label: 'rotated link', url: linkFor(token) })
      void qc.invalidateQueries({ queryKey: ['auth', 'grants'] })
    },
  })

  if (grantsQ.error) {
    return (
      <main className="admin-page">
        <SiteNav />
        <h1>Share links</h1>
        <p>{grantsQ.error.message === 'staff only' ? 'This console is staff-only.' : grantsQ.error.message}</p>
      </main>
    )
  }

  const canMint = !!(name.trim() || memo.trim())

  const grants = grantsQ.data?.grants ?? []
  const revokedCount = grants.filter(g => g.revokedAt).length
  const shown = showRevoked ? grants : grants.filter(g => !g.revokedAt)
  return (
    <main className="admin-page">
      <SiteNav />
      <h1>Share links</h1>
      <p>
        Revocable view links for people outside the SSO/whitelist set. The raw link is shown{' '}
        <strong>once</strong>, when it's created; revoking a link signs out everyone using it, on their next request.{' '}
        Per-email access lives in the <Link to="/admin/db/allowed_emails">allowlist table</Link> (all tables:{' '}
        <Link to="/admin/db">/admin/db</Link>).
      </p>
      <form
        className="mint"
        onSubmit={e => {
          e.preventDefault()
          if (canMint) mint.mutate()
        }}
      >
        <div className="field">
          <label htmlFor="mint-name">Name</label>
          <input id="mint-name" value={name} onChange={e => setName(e.target.value)} placeholder="full name" />
          <span className="hint">optional — the person the link signs in as; shown as their name (with the avatar below) while they browse</span>
        </div>
        <div className="field">
          <label htmlFor="mint-email">Email</label>
          <input id="mint-email" type="email" value={email} onChange={e => setEmail(e.target.value)} />
          <span className="hint">optional — binds the link to this address on first redeem (magic-link semantics)</span>
        </div>
        <div className="field avatar">
          <label htmlFor="mint-avatar">Avatar URL</label>
          <div className="row">
            <input id="mint-avatar" type="url" value={avatar} onChange={e => setAvatar(e.target.value)} placeholder="https://…" />
            {avatar.trim() && (
              <img className="avatar-preview" src={avatar.trim()} alt="" onError={e => { e.currentTarget.style.visibility = 'hidden' }} onLoad={e => { e.currentTarget.style.visibility = 'visible' }} />
            )}
          </div>
          <span className="hint">optional — the direct <code>https:</code> image URL of their avatar</span>
        </div>
        <div className="field">
          <label htmlFor="mint-memo">Memo</label>
          <input id="mint-memo" value={memo} onChange={e => setMemo(e.target.value)} />
          <span className="hint">optional — a label for you (e.g. where it's shared); with the holder and creator shown below, a person link needs none</span>
        </div>
        <div className="field">
          <label htmlFor="mint-ro">Read-only</label>
          <input id="mint-ro" type="checkbox" checked={readOnly} onChange={e => setReadOnly(e.target.checked)} />
          <span className="hint">on = view only (recommended for guests); off = a full viewer that can also stage deletions</span>
        </div>
        <div className="field">
          <label htmlFor="mint-days">Expiry</label>
          <input id="mint-days" className="days" value={days} onChange={e => setDays(e.target.value)} inputMode="numeric" size={4} />
          <span className="hint">days until the link stops working; blank = never</span>
        </div>
        <div className="field submit">
          <button type="submit" disabled={mint.isPending || !canMint}>Create link</button>
          {!canMint && <span className="hint">add a name or a memo first</span>}
          {mint.error && <span className="err">{mint.error.message}</span>}
        </div>
      </form>
      {minted && (
        <div className="minted">
          <p>
            Link <strong>{minted.label}</strong> — copy it now; it won't be shown again:
          </p>
          <div className="token-row">
            <code>{minted.url}</code>
            <button type="button" onClick={() => void navigator.clipboard.writeText(minted.url)}>copy</button>
          </div>
        </div>
      )}
      {revokedCount > 0 && (
        <label className="grants-toolbar">
          <input type="checkbox" checked={showRevoked} onChange={e => toggleRevoked(e.target.checked)} />
          show revoked <span className="dim">({revokedCount})</span>
        </label>
      )}
      <div className="table-scroll">
      <table className="grants">
        <thead>
          <tr>
            <th>memo</th><th>holder</th><th>scopes</th><th>redeems</th><th>last used</th><th>expires</th><th>created</th><th></th>
          </tr>
        </thead>
        <tbody>
          {shown.map(g => (
            <tr key={g.id} className={g.revokedAt ? 'revoked' : ''}>
              <td>{g.note ?? <em>—</em>}</td>
              <td>
                <span className="holder">
                  {g.subject?.avatar && <img className="grant-avi" src={g.subject.avatar} alt="" />}
                  {holderName(g) ?? <em>—</em>}
                </span>
              </td>
              <td>{g.scopes.join(' ')}</td>
              <td>{g.redeems}{g.maxRedeems != null ? `/${g.maxRedeems}` : ''}</td>
              <td>{fmtTs(g.lastUsedAt)}</td>
              <td>{fmtTs(g.expiresAt)}</td>
              <td>{fmtTs(g.createdAt)}<div className="by">{g.createdBy}</div></td>
              <td>
                {g.revokedAt
                  ? <span className="revoked-label">revoked {fmtTs(g.revokedAt)}</span>
                  : (
                    <div className="row-actions">
                      <button type="button" className="icon-btn" title="Rotate: re-key this link — new URL, same grant; the old link stops working" aria-label="Rotate link" onClick={() => rotate.mutate(g.id)} disabled={rotate.isPending}><RotateIcon /></button>
                      <button type="button" className="icon-btn danger" title="Revoke: kill this link — signs out everyone using it, on their next request" aria-label="Revoke link" onClick={() => revoke.mutate(g.id)} disabled={revoke.isPending}><RevokeIcon /></button>
                    </div>
                  )}
              </td>
            </tr>
          ))}
          {!shown.length && !grantsQ.isPending && (
            <tr><td colSpan={8}><em>{grants.length ? 'no active links (all revoked)' : 'no links created yet'}</em></td></tr>
          )}
        </tbody>
      </table>
      </div>
    </main>
  )
}
