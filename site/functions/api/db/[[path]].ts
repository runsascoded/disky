/**
 * Generic table CRUD over the registry in `_lib/tables.ts` — the whole admin
 * "editable tables" backend. Table/column names only ever come from the
 * registry (never the client); values are always bound parameters.
 *
 *   GET    /api/db            → tables the caller can read (+canWrite flags)
 *   GET    /api/db/<t>        → { spec, rows, canWrite }   (?limit=N, default 500)
 *   POST   /api/db/<t>        → insert { values: {col: v} }
 *   PATCH  /api/db/<t>        → update { pk, col, value }  (editable cols only)
 *   DELETE /api/db/<t>?pk=…   → delete row
 *
 * Every write appends old/new row JSON to `admin_edits`.
 */
import { type Ctx, identify, json } from '../../_lib/auth.js'
import { TABLES, type TableSpec, tableSpec } from '../../_lib/tables.js'

const now = () => Math.floor(Date.now() / 1000)

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const { request, env } = ctx
  if (!env.DB) return json({ error: 'db not configured' }, 503)
  const id = await identify(ctx)
  if (!id) return json({ error: 'unauthenticated' }, 401)
  const scopes = new Set(id.scopes)
  const has = (s: string) => scopes.has(s) || scopes.has('*')
  const who = id.email ?? id.name ?? 'unknown'

  const rest = new URL(request.url).pathname.replace(/^\/api\/db\/?/, '')
  if (!rest) {
    const tables = TABLES.filter(t => has(t.readScope)).map(t => ({
      name: t.name, pk: t.pk, columns: t.columns, desc: t.desc, orderBy: t.orderBy,
      canWrite: t.writeScope != null && has(t.writeScope),
    }))
    return json({ tables })
  }

  const spec = tableSpec(rest)
  if (!spec) return json({ error: 'unknown table' }, 404)
  if (!has(spec.readScope)) return json({ error: 'forbidden' }, 403)
  const canWrite = spec.writeScope != null && has(spec.writeScope)

  if (request.method === 'GET') {
    const limit = Math.min(Number(new URL(request.url).searchParams.get('limit')) || 500, 5000)
    const cols = spec.columns.map(c => c.name).join(', ')
    const q = env.DB.prepare(`SELECT ${cols} FROM ${spec.name} ORDER BY ${spec.orderBy} LIMIT ?`).bind(limit)
    // A D1 read can fail transiently (the console saw a bare 500 on
    // 2026-09-11 while two executors were inserting): retry once, and answer
    // with the cause — logged too, so it survives in Workers Logs.
    let results
    try {
      ({ results } = await q.all())
    } catch (e1) {
      try {
        ({ results } = await q.all())
      } catch (e2) {
        const msg = String((e2 as Error).message ?? e2)
        console.error(`db read ${spec.name} failed twice: ${String((e1 as Error).message ?? e1)} / ${msg}`)
        return json({ error: `D1 read failed: ${msg}` }, 500)
      }
    }
    return json({ spec: { ...spec, normalize: undefined, validate: undefined, canWrite }, rows: results })
  }

  if (!canWrite) return json({ error: 'forbidden' }, 403)
  const audit = (pk: string, action: string, old: unknown, new_: unknown) =>
    env.DB!.prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)')
      .bind(spec.name, pk, action, who, now(), old ? JSON.stringify(old) : null, new_ ? JSON.stringify(new_) : null)
  const fetchRow = (pk: string) =>
    env.DB!.prepare(`SELECT * FROM ${spec.name} WHERE ${spec.pk} = ?`).bind(pk).first()

  if (request.method === 'POST') {
    const { values = {} } = (await request.json()) as { values: Record<string, string> }
    const row: Record<string, string | number> = {}
    for (const c of spec.columns) {
      if (c.server === 'who') { row[c.name] = who; continue }
      if (c.server === 'now') { row[c.name] = now(); continue }
      let v = values[c.name]?.toString().slice(0, 2048) ?? ''
      if (spec.normalize) v = spec.normalize(c.name, v)
      if (!v) {
        if (c.required) return json({ error: `${c.name} required` }, 400)
        continue
      }
      const err = spec.validate?.(c.name, v)
      if (err) return json({ error: err }, 400)
      row[c.name] = c.type === 'int' ? Number(v) : v
    }
    const pk = String(row[spec.pk] ?? '')
    if (!pk) return json({ error: `${spec.pk} required` }, 400)
    if (await fetchRow(pk)) return json({ error: 'row already exists' }, 409)
    const names = Object.keys(row)
    await env.DB.batch([
      env.DB.prepare(`INSERT INTO ${spec.name} (${names.join(', ')}) VALUES (${names.map(() => '?').join(', ')})`)
        .bind(...names.map(n => row[n])),
      audit(pk, 'insert', null, row),
    ])
    return json({ ok: true, row })
  }

  if (request.method === 'PATCH') {
    const { pk, col, value } = (await request.json()) as { pk: string; col: string; value: string }
    const cspec = spec.columns.find(c => c.name === col)
    if (!cspec?.editable) return json({ error: `${col} is not editable` }, 400)
    let v = (value ?? '').toString().slice(0, 2048)
    if (spec.normalize) v = spec.normalize(col, v)
    if (v) {
      const err = spec.validate?.(col, v)
      if (err) return json({ error: err }, 400)
    } else if (cspec.required) return json({ error: `${col} required` }, 400)
    const old = await fetchRow(pk)
    if (!old) return json({ error: 'no such row' }, 404)
    await env.DB.batch([
      env.DB.prepare(`UPDATE ${spec.name} SET ${col} = ? WHERE ${spec.pk} = ?`)
        .bind(v ? (cspec.type === 'int' ? Number(v) : v) : null, pk),
      audit(pk, 'update', old, { ...old, [col]: v || null }),
    ])
    return json({ ok: true })
  }

  if (request.method === 'DELETE') {
    const pk = new URL(request.url).searchParams.get('pk') ?? ''
    const old = await fetchRow(pk)
    if (!old) return json({ error: 'no such row' }, 404)
    await env.DB.batch([
      env.DB.prepare(`DELETE FROM ${spec.name} WHERE ${spec.pk} = ?`).bind(pk),
      audit(pk, 'delete', old, null),
    ])
    return json({ ok: true })
  }

  return json({ error: 'method not allowed' }, 405)
}
