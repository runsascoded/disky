import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Tooltip } from './Tooltip'
import { NO_AUTOFILL } from './noAutofill'

/** `GET /api/admins` (`functions/api/admins.ts`): every path to the `admin` scope on this deployment. */
export interface AdminsData {
  /** Admin is the explicit list alone (`ADMIN_LIST`); off, the staff domain is an admin path. */
  list: boolean
  staffDomain: string | null
  /** The list's table: there, absent from this database, or not read by this deployment. */
  table: 'ok' | 'missing' | 'unused'
  rows: { email: string; note: string | null; who: string; ts: number; github: string | null }[]
  /** Break-glass addresses (the deploy setting `ADMIN_BOOTSTRAP`). */
  bootstrap: string[]
  none: boolean
  /** Live share links minted with the `admin` scope. */
  grants: { id: string; holder: string | null; email: string | null; scopes: string[]; createdBy: string; createdAt: number; expiresAt: number | null }[]
  /** The caller's email (null: a link). */
  you: string | null
}

export type AdminVia = 'list' | 'break-glass' | 'link' | 'staff domain'

/** One admin path, as the table shows it. */
export interface AdminPath {
  key: string
  /** The address, the link's holder, or the domain rule. */
  who: string
  via: AdminVia
  /** A listed address's GitHub handle, where recorded (what the list is compared with an org's owners by). */
  github: string | null
  note: string | null
  addedBy: string | null
  ts: number | null
  you: boolean
  /** A listed address that may be removed here; `false` with `why` for the last one. */
  removable: boolean
  why: string | null
}

export const VIA_TIP: Record<AdminVia, string> = {
  list: 'On the admin list: added here, removable here.',
  'break-glass': 'Named by the deploy setting ADMIN_BOOTSTRAP: an admin whatever the list holds. Changed in the deployment’s secrets, not here.',
  link: 'A share link minted with the admin scope: whoever holds it is an admin, whatever the list says. Revoke it under Share links.',
  'staff domain': 'Everyone who signs in on this domain is an admin (the admin list is off for this deployment).',
}

/** Every admin path in table order: the domain rule (list off), the break-glass addresses, the listed ones, the admin
 *  links. With the list on and no break-glass address, the last listed one can't be removed. */
export function adminPaths(d: AdminsData): AdminPath[] {
  const lastStays = d.list && !d.bootstrap.length && d.rows.length === 1
  return [
    ...(d.staffDomain ? [{ key: 'domain', who: `everyone @${d.staffDomain}`, via: 'staff domain' as const, github: null, note: null, addedBy: 'deploy config', ts: null, you: !!d.you?.endsWith(`@${d.staffDomain}`), removable: false, why: null }] : []),
    ...d.bootstrap.map(e => ({ key: `b:${e}`, who: e, via: 'break-glass' as const, github: null, note: null, addedBy: 'deploy config', ts: null, you: e === d.you, removable: false, why: null })),
    ...d.rows.map(r => ({
      key: `l:${r.email}`, who: r.email, via: 'list' as const, github: r.github, note: r.note, addedBy: r.who, ts: r.ts, you: r.email === d.you,
      removable: !lastStays, why: lastStays ? 'The last admin: add another before removing this one.' : null,
    })),
    ...d.grants.map(g => ({ key: `g:${g.id}`, who: g.holder ?? `link ${g.id.slice(0, 8)}`, via: 'link' as const, github: null, note: g.scopes.join(' '), addedBy: g.createdBy, ts: g.createdAt, you: false, removable: false, why: null })),
  ]
}

/** What the section says above the table when the list names nobody (null: nothing to say). */
export function emptyNotice(d: AdminsData): string | null {
  if (!d.list || d.rows.length) return null
  const how = d.table === 'missing' ? 'its table isn’t in this database yet (apply the `admin-list` recipe), so nothing can be added until it is' : 'add admins below'
  return `The admin list is empty: ${how}. You are an admin by ${d.you && d.bootstrap.includes(d.you) ? 'the break-glass setting (ADMIN_BOOTSTRAP)' : 'a link'}, and nobody else is one by the list.`
}

const fmtTs = (ts: number | null): string => (ts ? new Date(ts * 1000).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—')

const KEY = ['admins']

/** A listed admin's GitHub handle, editable in place: click to edit, Enter or blur saves (`PATCH /api/admins`), Escape
 *  cancels; blank clears it. */
function HandleCell({ handle, onSave, saving }: { handle: string | null; onSave: (github: string) => void; saving: boolean }) {
  const [draft, setDraft] = useState<string | null>(null)
  if (draft == null) {
    return (
      <button type="button" className="memo-edit" aria-label="Edit GitHub handle" onClick={() => setDraft(handle ?? '')} disabled={saving}>
        {handle ? `@${handle}` : <em>—</em>}
      </button>
    )
  }
  const save = () => {
    const next = draft.trim().replace(/^@/, '')
    setDraft(null)
    if (next !== (handle ?? '')) onSave(next)
  }
  return (
    <input
      className="memo-input" autoFocus value={draft} placeholder="GitHub handle" aria-label="GitHub handle" {...NO_AUTOFILL}
      onChange={e => setDraft(e.target.value)}
      onBlur={save}
      onKeyDown={e => {
        if (e.key === 'Enter') save()
        else if (e.key === 'Escape') setDraft(null)
      }}
    />
  )
}

async function call(init?: RequestInit, query = ''): Promise<unknown> {
  const r = await fetch(`/api/admins${query}`, { credentials: 'include', cache: 'no-store', ...init })
  const body = await r.json().catch(() => null) as { error?: string } | null
  if (!r.ok) throw Object.assign(new Error(body?.error ?? `admins: ${r.status}`), { status: r.status, body })
  return body
}

/** `/admin`'s "Admins" section: who is an admin and by what, with add and remove for the list. */
export function Admins() {
  const qc = useQueryClient()
  const q = useQuery<AdminsData, Error>({ queryKey: KEY, queryFn: () => call() as Promise<AdminsData>, retry: false })
  const [email, setEmail] = useState('')
  const [note, setNote] = useState('')
  const [github, setGithub] = useState('')
  // Removing yourself takes a second click on the same row.
  const [sure, setSure] = useState<string | null>(null)
  const done = () => { setSure(null); void qc.invalidateQueries({ queryKey: KEY }) }
  const add = useMutation<unknown, Error>({
    mutationFn: () => call({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email, github, note }) }),
    onSuccess: () => { setEmail(''); setNote(''); setGithub(''); done() },
  })
  const setHandle = useMutation<unknown, Error, { email: string; github: string }>({
    mutationFn: b => call({ method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b) }),
    onSuccess: done,
  })
  const remove = useMutation<unknown, Error, string>({
    mutationFn: e => call({ method: 'DELETE' }, `?email=${encodeURIComponent(e)}`),
    onSuccess: done,
  })
  if (q.error) return <section className="admins"><h2>Admins</h2><p className="err">{q.error.message}</p></section>
  if (!q.data) return null
  const d = q.data
  const paths = adminPaths(d)
  const notice = emptyNotice(d)
  const editable = d.table === 'ok'
  return (
    <section className="admins">
      <h2>Admins</h2>
      <p>
        {d.list
          ? <>Admins are the addresses listed here and nobody else: being staff doesn’t make someone one. An admin can dispatch deletions, mint links and edit this list.</>
          : <>Everyone who signs in{d.staffDomain ? <> at <code>@{d.staffDomain}</code></> : null} as staff is an admin{d.table === 'unused' ? '.' : '; the addresses listed below are too.'}</>}
        {' '}Every path to admin is in this table. A change takes effect on that person’s next request and is logged (<Link to="/admin/db/admin_edits">admin_edits</Link>).
      </p>
      {notice && <p className="admins-notice" role="alert">{notice}</p>}
      <div className="table-scroll">
        <table className="grants admins-table">
          <thead><tr><th>admin</th><th><Tooltip content="Their GitHub handle, where known: what this list is compared with a GitHub org’s owners by (dt-cloud admins check). Click a listed admin’s to edit it."><span className="admin-via">GitHub</span></Tooltip></th><th>by</th><th>note</th><th>added by</th><th>when</th><th /></tr></thead>
          <tbody>
            {paths.map(p => (
              <tr key={p.key}>
                <td>{p.via === 'link' || p.via === 'staff domain' ? p.who : <code>{p.who}</code>}{p.you && <span className="dim"> (you)</span>}</td>
                <td>{p.via === 'list' && editable ? <HandleCell handle={p.github} saving={setHandle.isPending} onSave={h => setHandle.mutate({ email: p.who, github: h })} /> : ''}</td>
                <td><Tooltip content={VIA_TIP[p.via]}><span className={`admin-via via-${p.via.replace(' ', '-')}`}>{p.via}</span></Tooltip></td>
                <td>{p.note ?? ''}</td>
                <td>{p.addedBy ?? '—'}</td>
                <td>{fmtTs(p.ts)}</td>
                <td>
                  {p.via === 'list' && editable && (p.removable
                    ? (
                      <button
                        type="button" className="icon-btn danger" disabled={remove.isPending}
                        onClick={() => (p.you && sure !== p.who ? setSure(p.who) : remove.mutate(p.who))}
                      >{p.you && sure === p.who ? 'remove yourself?' : 'remove'}</button>
                    )
                    : <Tooltip content={p.why}><span><button type="button" className="icon-btn danger" disabled>remove</button></span></Tooltip>)}
                </td>
              </tr>
            ))}
            {!paths.length && <tr><td colSpan={7}><em>nobody is an admin</em></td></tr>}
          </tbody>
        </table>
      </div>
      {(remove.error ?? setHandle.error) && <p className="err">{(remove.error ?? setHandle.error)!.message}</p>}
      {editable && (
        <form className="admins-add" onSubmit={e => { e.preventDefault(); if (email.trim()) add.mutate() }}>
          <input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="email they sign in with" aria-label="Admin’s email" {...NO_AUTOFILL} />
          <input value={github} onChange={e => setGithub(e.target.value)} placeholder="GitHub handle (optional)" aria-label="GitHub handle" {...NO_AUTOFILL} />
          <input value={note} onChange={e => setNote(e.target.value)} placeholder="note (optional)" aria-label="Note" />
          <button type="submit" disabled={add.isPending || !email.trim()}>Add admin</button>
          {add.error && <span className="err">{add.error.message}</span>}
        </form>
      )}
    </section>
  )
}

/** What a signed-in visitor who isn't an admin sees on `/admin`: that it's the admins' console, and, when the list is
 *  on and names nobody, that this is why (with the way out). */
export function AdminsRefusal() {
  const q = useQuery<{ none: boolean }, Error>({
    queryKey: [...KEY, 'refusal'],
    retry: false,
    queryFn: async () => {
      const r = await fetch('/api/admins', { credentials: 'include', cache: 'no-store' })
      const body = await r.json().catch(() => null) as { none?: boolean } | null
      return { none: r.status === 403 && body?.none === true }
    },
  })
  return (
    <>
      <p>This console is for admins.</p>
      {q.data?.none && (
        <p className="admins-notice" role="alert">
          Nobody is an admin of this deployment: its admin list is on and empty. Naming one address in the deploy setting <code>ADMIN_BOOTSTRAP</code> makes that person an admin, who can then add the rest here.
        </p>
      )}
    </>
  )
}
