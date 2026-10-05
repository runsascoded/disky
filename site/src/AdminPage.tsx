import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { AvatarField } from '@open-athena/auth/react'
import { Link, useSearchParams } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { DEFAULT_STORE } from './stores'
import { useDocTitle } from './title'
import { PreviewLinks } from './PreviewLinks'

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

/** A grant's memo (`note`, the admin-side label), editable in place: click to
 *  edit, Enter or blur saves (`PATCH /api/auth/grants/:id`), Escape cancels. */
function MemoCell({ grant, onSave, saving }: { grant: Grant; onSave: (note: string | null) => void; saving: boolean }) {
  const [draft, setDraft] = useState<string | null>(null)
  if (grant.revokedAt) return <>{grant.note || <em>—</em>}</>
  if (draft == null) {
    return (
      <button type="button" className="memo-edit" aria-label="Edit memo" onClick={() => setDraft(grant.note ?? '')} disabled={saving}>
        {grant.note || <em>—</em>}
      </button>
    )
  }
  const save = () => {
    const note = draft.trim() || null
    setDraft(null)
    if (note !== (grant.note || null)) onSave(note)
  }
  return (
    <input
      className="memo-input"
      autoFocus
      value={draft}
      placeholder="memo"
      onChange={e => setDraft(e.target.value)}
      onBlur={save}
      onKeyDown={e => {
        if (e.key === 'Enter') save()
        else if (e.key === 'Escape') setDraft(null)
      }}
    />
  )
}

const linkFor = (token: string): string => `${window.location.origin}/?key=${token}`

/** `POST /api/auth/grants`' allowlist outcome for a link minted with `allowlist: true`. */
interface Allowed { email: string; status: 'added' | 'widened' | 'already' }

/** What a link (and, with an email, its holder's account) may do: view; view
 *  and stage deletes; or — the account only, never the link — administer. */
type Access = 'read' | 'view' | 'admin'
/** The admin step's outcome (an `admin_emails` row for the account). */
type AdminResult = 'added' | 'already' | { error: string }

/** Who a link is for: the person (`subject.name`, then their email), else the
 * grant's admin `name` — the fallback for CLI/agent-token grants. */
const holderName = (g: Grant): string | null => g.subject?.name || g.subject?.email || g.name || null

/** A body for `PATCH /api/auth/grants/:id`: the memo, or the holder's name and
 *  face (`avatar` absent = keep it, null = clear, a `data:` URI = replace;
 *  copied server-side, as at mint). */
type GrantEdit = { note?: string | null; subjectName?: string | null; avatar?: string | null }

/** Who a link is for, editable in place: click to rename the holder or change
 *  their face (`<AvatarField>`, the mint form's picker), Save or Cancel. */
function HolderCell({ grant, onSave, saving, error }: { grant: Grant; onSave: (edit: GrantEdit, done: () => void) => void; saving: boolean; error: string | null }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  // `undefined` = keep the current face; null = clear it; a `data:` URI = replace.
  const [avatar, setAvatar] = useState<string | null | undefined>(undefined)
  const face = (
    <span className="holder">
      {grant.subject?.avatar && <img className="grant-avi" src={grant.subject.avatar} alt="" />}
      {holderName(grant) ?? <em>—</em>}
    </span>
  )
  if (grant.revokedAt) return face
  if (!editing) {
    return (
      <button type="button" className="memo-edit" aria-label="Edit holder" onClick={() => { setName(grant.subject?.name ?? ''); setAvatar(undefined); setEditing(true) }}>
        {face}
      </button>
    )
  }
  return (
    <form
      className="holder-edit"
      onSubmit={e => {
        e.preventDefault()
        onSave({ subjectName: name.trim() || null, ...(avatar === undefined ? {} : { avatar }) }, () => setEditing(false))
      }}
      onKeyDown={e => { if (e.key === 'Escape') setEditing(false) }}
    >
      <input className="memo-input" autoFocus value={name} placeholder="holder’s name" onChange={e => setName(e.target.value)} />
      <AvatarField
        id={`holder-avatar-${grant.id}`}
        endpoint="/api/auth/avatar"
        value={avatar === undefined ? (grant.subject?.avatar ?? null) : avatar}
        onChange={setAvatar}
        email={grant.subject?.email ?? grant.email}
        autoGravatar={false}
        name={name.trim() || null}
        size={32}
      />
      {error && <p className="err">{error}</p>}
      <div className="row-actions">
        <button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        <button type="button" onClick={() => setEditing(false)}>Cancel</button>
      </div>
    </form>
  )
}

// Persist the in-progress mint form so a reload / redeploy doesn't wipe a draft.
// Per-tab (sessionStorage), cleared once the link is minted.
const DRAFT_KEY = 'admin-mint-draft'
interface Draft {
  memo: string
  name: string
  email: string
  /** A face's `data:` URI (null: none). */
  avatar: string | null
  access: Access
  days: string
  /** Pre-`access` drafts. */
  readOnly?: boolean
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
  // A face as the `data:` URI `<AvatarField>` produced (copied server-side at
  // mint, never a hotlink); null = none.
  const [avatar, setAvatar] = useState<string | null>(draft.avatar || null)
  const [days, setDays] = useState(draft.days ?? '30')
  const [access, setAccess] = useState<Access>(draft.access ?? (draft.readOnly === false ? 'view' : 'read'))
  // Admin is an account's, so it needs an email; without one it reads as Viewer.
  const effAccess: Access = access === 'admin' && !email.trim() ? 'view' : access
  const [minted, setMinted] = useState<{ label: string; url: string; allowed: Allowed | null; admin: AdminResult | null } | null>(null)

  useEffect(() => {
    try {
      // A face is a few-KB `data:` URI; keep it in the draft unless it's oversized.
      const face = avatar && avatar.length <= 32_000 ? avatar : null
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({ memo, name, email, avatar: face, access, days }))
    } catch {
      // sessionStorage can throw (private mode / disabled) — a lost draft is cosmetic.
    }
  }, [memo, name, email, avatar, access, days])

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
          avatar,
          // An email makes it a person's link: their account is created (or
          // updated) with the link's access, so they can also sign in directly.
          allowlist: !!email.trim(),
          // The link itself is never admin: an admin account still has to sign in.
          scopes: [effAccess === 'read' ? `${DEFAULT_STORE.key}:read` : DEFAULT_STORE.key],
          expiresInS,
        }),
      })
      if (!r.ok) {
        // A refused face (`400 { error: 'invalid avatar', detail }`) says why.
        const body = await r.json().catch(() => null) as { error?: string; detail?: string } | null
        throw new Error(body?.detail ? `${body.error ?? 'create failed'}: ${body.detail}` : `create failed: ${r.status}`)
      }
      const out = await r.json() as { grant: Grant; token: string; allowed?: Allowed }
      // Admin: the account's `admin_emails` row (409 = already one). The link
      // stands either way; a failure here is reported beside it.
      let admin: AdminResult | null = null
      if (effAccess === 'admin') {
        const a = await fetch('/api/db/admin_emails', {
          method: 'POST',
          credentials: 'include',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ values: { email: email.trim(), note: memo.trim() ? `with link "${memo.trim()}"` : 'with a share link' } }),
        })
        admin = a.ok ? 'added' : a.status === 409 ? 'already' : { error: `admin: ${a.status}` }
      }
      return { ...out, admin }
    },
    onSuccess: ({ grant, token, allowed, admin }) => {
      setMinted({ label: holderName(grant) ?? grant.note ?? 'unnamed', url: linkFor(token), allowed: allowed ?? null, admin })
      setMemo('')
      setName('')
      setEmail('')
      setAvatar(null)
      setAccess('read')
      try {
        sessionStorage.removeItem(DRAFT_KEY)
      } catch {
        // ignore
      }
      void qc.invalidateQueries({ queryKey: ['auth', 'grants'] })
    },
  })

  const editGrant = useMutation({
    mutationFn: async ({ id, edit }: { id: string; edit: GrantEdit; done?: () => void }) => {
      const r = await fetch(`/api/auth/grants/${id}`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(edit),
      })
      if (!r.ok) {
        // a refused face (e.g. a LinkedIn URL) comes back as 400 `{error, detail}`
        const b = await r.json().catch(() => null) as { error?: string; detail?: string } | null
        throw new Error(b?.detail ?? b?.error ?? `update failed: ${r.status}`)
      }
    },
    onSuccess: (_, { done }) => {
      done?.()
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
      setMinted({ label: 'rotated link', url: linkFor(token), allowed: null, admin: null })
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
        A link with an email is a person's: it also creates their account, listed under <Link to="/admin/db/allowed_emails">users</Link> (all tables:{' '}
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
          <span className="hint">
            optional — makes it a person's link: binds it to this address, and creates their account so they can also sign in directly (Google or an emailed code).
            The account doesn't expire or get revoked with the link; remove it from <Link to="/admin/db/allowed_emails">users</Link>
          </span>
        </div>
        <div className="field avatar">
          <label htmlFor="mint-avatar">Face</label>
          <AvatarField id="mint-avatar" endpoint="/api/auth/avatar" value={avatar} onChange={setAvatar} email={email.trim() || null} name={name.trim() || null} size={40} />
          <span className="hint">optional — paste a GitHub / Bluesky / Mastodon profile or an image address, or upload; with an email and nothing else, their Gravatar. Copied and stored, never hotlinked</span>
        </div>
        <div className="field">
          <label htmlFor="mint-memo">Memo</label>
          <input id="mint-memo" value={memo} onChange={e => setMemo(e.target.value)} />
          <span className="hint">optional — a label for you (e.g. where it's shared); with the holder and creator shown below, a person link needs none</span>
        </div>
        <div className="field">
          <label htmlFor="mint-access">Access</label>
          <select id="mint-access" value={effAccess} onChange={e => setAccess(e.target.value as Access)}>
            <option value="read">Read-only</option>
            <option value="view">Viewer — can also stage deletions</option>
            <option value="admin" disabled={!email.trim()}>Admin{email.trim() ? '' : ' (needs an email)'}</option>
          </select>
          <span className="hint">
            {effAccess === 'admin'
              ? <><b>Admin applies to their account only</b>: the link itself is a Viewer; signed in as this email, they can dispatch deletes and mint links</>
              : <>the link's access{email.trim() ? ', and their account’s' : ''}; Read-only is right for most guests</>}
          </span>
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
          {minted.allowed && (
            <p className="allowed">
              <code>{minted.allowed.email}</code>{' '}
              {minted.allowed.status === 'added' ? 'now has an account: they can also sign in directly'
                : minted.allowed.status === 'widened' ? 'already had an account, upgraded to this link’s access'
                : 'already has an account'}
              {minted.admin === 'added' && <>, and is now an <b>admin</b></>}
              {minted.admin === 'already' && <>, and was already an admin</>}
              {minted.admin && typeof minted.admin === 'object' && <span className="err"> — couldn’t make them admin ({minted.admin.error})</span>}
            </p>
          )}
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
              <td className="memo"><MemoCell grant={g} onSave={note => editGrant.mutate({ id: g.id, edit: { note } })} saving={editGrant.isPending} /></td>
              <td>
                <HolderCell
                  grant={g}
                  onSave={(edit, done) => editGrant.mutate({ id: g.id, edit, done })}
                  saving={editGrant.isPending && editGrant.variables?.id === g.id}
                  error={editGrant.isError && editGrant.variables?.id === g.id ? editGrant.error.message : null}
                />
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
      <PreviewLinks />
    </main>
  )
}
