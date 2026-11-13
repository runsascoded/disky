/**
 * The deployment's admins (`/admin`'s "Admins" section): every path to the `admin` scope, and the editor of the one
 * that is a list.
 *
 *   GET    /api/admins            → { list, staffDomain, table, rows, handles, bootstrap, none, grants, you }
 *   POST   /api/admins            → add { email, github?, note? } to the list
 *   PATCH  /api/admins            → { email, github?, note? }: set a listed address's GitHub handle and/or note
 *   DELETE /api/admins?email=…    → remove a listed address
 *
 * Admin-gated (`requireAdmin`), all four. `rows` are the `admin_emails` rows with who added each and when, and the
 * person's GitHub handle where known (what `dt-cloud admins check -o <org>` compares with that org's owners);
 * `bootstrap` the break-glass addresses (`ADMIN_BOOTSTRAP`, deploy config: not editable here); `grants` the live
 * share links minted with the `admin` scope, which are admins by themselves, whatever the list says (revoke one on
 * `/admin`'s links table). With the list off (`list: false`) the staff domain (`staffDomain`) is an admin path too.
 *
 * A signed-in caller who isn't an admin gets 403 with `none`: whether the list is on and names nobody, so the page
 * can say why nobody is an admin instead of a bare refusal.
 *
 * Every change appends to `admin_edits` (table `admin_emails`), in the same statement batch as the write. With the
 * list on and no break-glass address, the last row can't be removed: the list never empties itself.
 *
 * On a database from before the `github` column (`handles: false`, the `admin-list` recipe unapplied) all of this works
 * but the handles: storing one answers `NO_HANDLES`, so a deployment has this editor from the merge on.
 */
import { ADMIN_SCOPE, adminListOn, adminRoster, type AdminRow, bootstrapAdmins, type Ctx, gateFor, identify, json, requireAdmin } from '../_lib/auth.js'
import { noteActivity } from '../_lib/activityHook.js'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
/** A GitHub login: letters, digits and single hyphens, at most 39 characters. */
const GITHUB_RE = /^[A-Za-z\d](?:[A-Za-z\d]|-(?=[A-Za-z\d])){0,38}$/
const now = () => Math.floor(Date.now() / 1000)

/** A live grant holding the `admin` scope: an admin path of its own. */
export interface AdminGrant {
  id: string
  /** Who it signs in as: the holder's name, else their email, else the link's label. */
  holder: string | null
  email: string | null
  scopes: string[]
  createdBy: string
  createdAt: number
  expiresAt: number | null
}

async function adminGrants(ctx: Ctx): Promise<AdminGrant[]> {
  const gate = gateFor(ctx.env, ctx.request)
  if (!gate) return []
  const at = now()
  return (await gate.list({ includeRevoked: false }))
    .filter(g => (g.scopes.includes(ADMIN_SCOPE) || g.scopes.includes('*')) && !g.revokedAt && (g.expiresAt == null || g.expiresAt > at))
    .map(g => ({ id: g.id, holder: g.subject?.name || g.email || g.name || null, email: g.email ?? null, scopes: g.scopes, createdBy: g.createdBy, createdAt: g.createdAt, expiresAt: g.expiresAt ?? null }))
}

const MISSING = { error: '`admin_emails` does not exist in this database: apply its recipe (`site/migrations/admin-list.sql`) first' }
const NO_HANDLES = { error: '`admin_emails` has no `github` column in this database: GitHub handles arrive with its recipe (`site/migrations/admin-list.sql`). Adding, removing and notes work without it' }

/** A handle as typed (`@name`, a profile URL's last segment is not parsed): trimmed, a leading `@` dropped; null when
 *  blank; an error when it isn't a GitHub login. */
function handleOf(v: unknown): string | null | { error: string } {
  const h = String(v ?? '').trim().replace(/^@/, '')
  if (!h) return null
  return GITHUB_RE.test(h) ? h : { error: 'not a GitHub handle' }
}

/** A row's JSON as `admin_edits` holds it: the columns the table has. */
const rowSql = (handles: boolean): string => `json_object('email', email, 'note', note, 'who', who, 'ts', ts${handles ? ", 'github', github" : ''})`
const rowJson = (r: AdminRow, handles: boolean): string => JSON.stringify({ email: r.email, note: r.note, who: r.who, ts: r.ts, ...(handles ? { github: r.github } : {}) })

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const { request, env } = ctx
  const id = await requireAdmin(ctx)
  if (id instanceof Response) {
    // Signed in but not an admin: say whether that is because nobody is.
    if (id.status !== 403 || !(await identify(ctx))) return id
    return json({ error: 'forbidden', none: (await adminRoster(env)).none }, 403)
  }
  const who = id.email ?? id.name ?? 'unknown'

  if (request.method === 'GET') {
    const roster = await adminRoster(env)
    return json({ ...roster, grants: await adminGrants(ctx), you: id.email?.toLowerCase() ?? null }, 200, { 'cache-control': 'private, no-store' })
  }
  if (!['POST', 'PATCH', 'DELETE'].includes(request.method)) return json({ error: 'method not allowed' }, 405)

  if (!env.DB) return json({ error: 'db not configured' }, 503)
  const db = env.DB
  const roster = await adminRoster(env)
  if (roster.table === 'unused') return json({ error: 'this deployment keeps no admin list (`ADMIN_LIST` / `ADMIN_EMAILS` unset)' }, 404)
  if (roster.table === 'missing') return json(MISSING, 503)
  return write(ctx, db, roster.rows, roster.handles, who, id.email?.toLowerCase() ?? null)
}

async function write(ctx: Ctx, db: NonNullable<Ctx['env']['DB']>, rows: AdminRow[], handles: boolean, who: string, you: string | null): Promise<Response> {
  const { request, env } = ctx
  const AUDIT = 'INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json)'

  if (request.method === 'POST') {
    const b = (await request.json().catch(() => ({}))) as { email?: unknown; github?: unknown; note?: unknown }
    const email = String(b.email ?? '').trim().toLowerCase().slice(0, 320)
    const note = String(b.note ?? '').trim().slice(0, 2048) || null
    const github = handleOf(b.github)
    if (!EMAIL_RE.test(email)) return json({ error: 'not an email address' }, 400)
    if (github && typeof github === 'object') return json(github, 400)
    if (github && !handles) return json(NO_HANDLES, 503)
    if (rows.some(r => r.email === email)) return json({ error: 'already an admin' }, 409)
    const row: AdminRow = { email, note, who, ts: now(), github }
    await db.batch([
      handles
        ? db.prepare('INSERT INTO admin_emails (email, note, who, ts, github) VALUES (?, ?, ?, ?, ?)').bind(row.email, row.note, row.who, row.ts, row.github)
        : db.prepare('INSERT INTO admin_emails (email, note, who, ts) VALUES (?, ?, ?, ?)').bind(row.email, row.note, row.who, row.ts),
      db.prepare(`${AUDIT} VALUES ('admin_emails', ?, 'insert', ?, ?, NULL, ?)`).bind(email, who, row.ts, rowJson(row, handles)),
    ])
    noteActivity(ctx, { line: `added \`${email}\` as an admin`, table: 'admin_emails', pk: email, action: 'insert' })
    return json({ ok: true, row })
  }

  if (request.method === 'PATCH') {
    const b = (await request.json().catch(() => ({}))) as { email?: unknown; github?: unknown; note?: unknown }
    const email = String(b.email ?? '').trim().toLowerCase()
    const old = rows.find(r => r.email === email)
    if (!old) return json({ error: 'not on the list' }, 404)
    const github = 'github' in b ? handleOf(b.github) : old.github
    if (github && typeof github === 'object') return json(github, 400)
    if (github && !handles) return json(NO_HANDLES, 503)
    const note = 'note' in b ? String(b.note ?? '').trim().slice(0, 2048) || null : old.note
    const row: AdminRow = { ...old, note, github }
    if (rowJson(row, handles) === rowJson(old, handles)) return json({ ok: true, row })
    // The log row first, read from the row it changes, so a row removed meanwhile logs nothing.
    const [, upd] = await db.batch([
      db.prepare(`${AUDIT} SELECT 'admin_emails', email, 'update', ?2, ?3, ${rowSql(handles)}, ?4 FROM admin_emails WHERE email = ?1`).bind(email, who, now(), rowJson(row, handles)),
      handles
        ? db.prepare('UPDATE admin_emails SET note = ?2, github = ?3 WHERE email = ?1').bind(email, note, github)
        : db.prepare('UPDATE admin_emails SET note = ?2 WHERE email = ?1').bind(email, note),
    ])
    if (!upd.meta.changes) return json({ error: 'not on the list' }, 404)
    noteActivity(ctx, { line: `admin \`${email}\`: ${github !== old.github ? `GitHub handle ${github ? `\`${github}\`` : 'cleared'}` : 'note edited'}`, table: 'admin_emails', pk: email, action: 'update' })
    return json({ ok: true, row })
  }

  const email = (new URL(request.url).searchParams.get('email') ?? '').trim().toLowerCase()
  if (!rows.some(r => r.email === email)) return json({ error: 'not on the list' }, 404)
  // The list on, with no break-glass address: its last row stays, or nobody could add the next.
  const mayEmpty = !adminListOn(env) || bootstrapAdmins(env).length > 0
  const keep = 'email = ?1 AND (?2 OR (SELECT COUNT(*) FROM admin_emails) > 1)'
  // The log row first, under the delete's own condition, so the pair is one decision in one transaction.
  const [, del] = await db.batch([
    db.prepare(`${AUDIT} SELECT 'admin_emails', email, 'delete', ?3, ?4, ${rowSql(handles)}, NULL FROM admin_emails WHERE ${keep}`).bind(email, mayEmpty ? 1 : 0, who, now()),
    db.prepare(`DELETE FROM admin_emails WHERE ${keep}`).bind(email, mayEmpty ? 1 : 0),
  ])
  if (!del.meta.changes) {
    // Removed by someone else meanwhile, or the guard refused.
    if (!(await db.prepare('SELECT email FROM admin_emails WHERE email = ?').bind(email).first())) return json({ error: 'not on the list' }, 404)
    const self = you === email
    return json({ error: `${self ? 'you are' : 'that is'} the last admin: add another before removing ${self ? 'yourself' : 'them'}`, last: true }, 409)
  }
  noteActivity(ctx, { line: `removed \`${email}\` from the admins`, table: 'admin_emails', pk: email, action: 'delete' })
  return json({ ok: true })
}
