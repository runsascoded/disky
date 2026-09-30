import { Link, useParams } from 'react-router-dom'
import { DbTable } from './DbTable'
import { useTables } from './db'
import { useDocTitle } from './title'

// /admin/db/:table — one registry table, editable for admins, read-only for
// anyone whose scopes cover its readScope (e.g. share the allowed_emails URL
// instead of a gist). /admin/db (no table) lists what the caller can see.
export function AdminDbPage() {
  const { table } = useParams()
  useDocTitle(table, 'DB', 'Admin')
  const { data: tables, error } = useTables()

  if (error) {
    return (
      <div className="admin-page">
        <h1>Tables</h1>
        <p className="err">{error.message === 'unauthenticated' ? 'Sign in to view this page.' : error.message}</p>
        <p><Link to="/">← back</Link></p>
      </div>
    )
  }

  const meta = tables?.find(t => t.name === table)
  return (
    <div className="admin-page">
      <p className="crumbs">
        <Link to="/">dashboard</Link> · <Link to="/admin">admin</Link>
        {table && <> · <Link to="/admin/db">tables</Link></>}
      </p>
      {table ? (
        <>
          <h1><code>{table}</code></h1>
          {meta && <p>{meta.desc}</p>}
          <DbTable name={table} />
        </>
      ) : (
        <>
          <h1>Tables</h1>
          <ul className="table-list">
            {(tables ?? []).map(t => (
              <li key={t.name}>
                <Link to={`/admin/db/${t.name}`}><code>{t.name}</code></Link>
                {!t.canWrite && <span className="ro-tag">read-only</span>}
                <span className="desc">{t.desc}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  )
}
