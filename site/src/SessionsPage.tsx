import { DefaultViewer, SessionList, SessionView, type SessionListParams } from '@open-athena/auth/react/session-log'
import '@open-athena/auth/react/session-log.css'
import type { SessionRow } from '@open-athena/auth/session-log'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useRegistry } from './identities'
import { useUserEmails } from './owners'
import { viewerOf } from './sessionsModel'
import { SiteNav } from './SiteNav'
import { UserChip } from './UserChip'
import { useDocTitle } from './title'

function Viewer({ session }: { session: SessionRow }) {
  const viewer = viewerOf(session, useUserEmails(true), useRegistry())
  return <span className="session-who">
    {viewer.kind === 'user'
      ? <><UserChip who={viewer.id} extra={<div className="uc-sub">{viewer.email}</div>} />{viewer.guest && ' · guest link'}</>
      : <DefaultViewer s={session} />}
  </span>
}
const renderViewer = (session: SessionRow) => <Viewer session={session} />

/** Routing, URL filters and disky user chips around the package's shared list and timeline. */
export function SessionsPage() {
  const { sid } = useParams()
  const [search, setSearch] = useSearchParams()
  useDocTitle(sid ? `session ${sid.slice(0, 8)}` : 'Sessions', 'Admin')
  const params: SessionListParams = {
    email: search.get('u') ?? '', since: search.get('since') ?? '', errors: search.get('err') === '1',
    after: search.get('after'), before: search.get('before'),
  }
  const onParams = (next: SessionListParams, { replace }: { replace: boolean }) => setSearch(prior => {
    const query = new URLSearchParams(prior)
    for (const [key, value] of Object.entries({ u: next.email, since: next.since, err: next.errors ? '1' : '', after: next.after, before: next.before })) {
      if (value) query.set(key, value)
      else query.delete(key)
    }
    return query
  }, { replace })
  return <div className="admin-page sessions-page">
    <SiteNav />
    <p className="crumbs"><Link to="/admin">admin</Link>{sid && <> · <Link to="/admin/sessions">sessions</Link></>}</p>
    {sid ? <SessionView sid={sid} renderViewer={renderViewer} /> : <SessionList
      params={params} onParams={onParams} sessionHref={id => `/admin/sessions/${encodeURIComponent(id)}`}
      renderLink={({ href, ...props }) => <Link to={href} {...props} />} renderViewer={renderViewer}
    />}
  </div>
}
