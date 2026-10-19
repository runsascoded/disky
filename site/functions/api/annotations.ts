/** Hand-written notes pinned to dates on the "Size over time" chart (spec `chart-annotations.md`).
 *
 *   GET    /api/annotations[?store=K]                   → { annotations: ChartAnnotation[], applied }  viewer of the store
 *   POST   /api/annotations[?store=K]  {at, text, path?, url?, kind?}  → 201 { annotation }           admin
 *   PATCH  /api/annotations[?store=K]  {id, …any of those}             → { annotation }               admin
 *   DELETE /api/annotations?id=N[&store=K]              → { deleted }                                  admin
 *
 * Notes are per store: `store=` as every data request carries it (none = the primary, `primary`), a secondary
 * store's key, or a union view's. A secondary store's notes need its viewer scope (`STORES_JSON` `scope`, e.g.
 * `/meta`'s `admin`), as its data does. A D1 without the table (migration `chart-annotations` not applied) reads
 * as `{annotations: [], applied: false}`, and a write is a 503 naming the migration. Not edge-cached: one indexed
 * D1 read, and an admin's write must show at once. */
import { type Ctx, json, requireAdmin, requireViewer } from '../_lib/auth.js'
import { PRIMARY_STORE, requestedStore, StoreConfigError, unionViews, withStore } from '../_lib/stores.js'
import { addNote, deleteNote, editNote, listNotes, missingScope, missingTable } from '../_lib/annotations.js'
import { type AnnotationsBody, type NoteFields, parseNote } from '../_lib/annotationShape.js'

export const NOT_APPLIED = 'chart annotations are not enabled here: apply D1 migration chart-annotations'
export const SCOPE_NOT_APPLIED = 'chart notes can’t be written here until D1 migration chart-annotation-scope is applied (path, url, kind)'
const NO_STORE = { 'cache-control': 'private, no-store' }

/** The request's store key, and the context its viewers are gated on (a secondary store's overlay env). */
function noteStore(ctx: Ctx): { key: string; ctx: Ctx } | Response {
  const key = requestedStore(ctx.request)
  if (key === null) return { key: PRIMARY_STORE, ctx }
  try {
    if (unionViews(ctx.env)[key]) return { key, ctx }
  } catch (e) {
    if (e instanceof StoreConfigError) return json({ error: e.message }, 500)
    throw e
  }
  const sc = withStore(ctx)
  return sc instanceof Response ? sc : { key, ctx: sc }
}

const now = () => Math.floor(Date.now() / 1000)

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const { request, env } = ctx
  if (!env.DB) return json({ error: 'annotations backend not configured (DB)' }, 503)
  const st = noteStore(ctx)
  if (st instanceof Response) return st
  const db = env.DB

  if (request.method === 'GET') {
    const id = await requireViewer(st.ctx)
    if (id instanceof Response) return id
    const rows = await listNotes(db, st.key)
    const body: AnnotationsBody = { annotations: rows ?? [], applied: rows !== null }
    return json(body, 200, NO_STORE)
  }

  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  // The store's own viewer scope too: an admin writes only notes they could read.
  const viewer = await requireViewer(st.ctx)
  if (viewer instanceof Response) return viewer
  const who = id.email ?? id.name ?? 'unknown'
  try {
    if (request.method === 'POST' || request.method === 'PATCH') {
      let body: unknown
      try {
        body = await request.json()
      } catch {
        return json({ error: 'want a JSON body' }, 400)
      }
      const note = parseNote(body, request.method === 'PATCH')
      if ('error' in note) return json(note, 400)
      if (request.method === 'POST') return json({ annotation: await addNote(db, st.key, note as NoteFields & { at: string; text: string }, who, now()) }, 201)
      const nid = (body as { id?: unknown }).id
      if (typeof nid !== 'number' || !Number.isInteger(nid)) return json({ error: 'id: want an integer' }, 400)
      const row = await editNote(db, st.key, nid, note, who, now())
      return row ? json({ annotation: row }) : json({ error: 'no such annotation' }, 404)
    }
    if (request.method === 'DELETE') {
      const raw = new URL(request.url).searchParams.get('id') ?? ''
      if (!/^\d+$/.test(raw)) return json({ error: 'id: want an integer' }, 400)
      const row = await deleteNote(db, st.key, Number(raw), who, now())
      return row ? json({ deleted: row }) : json({ error: 'no such annotation' }, 404)
    }
  } catch (e) {
    if (missingTable(e)) return json({ error: NOT_APPLIED }, 503)
    if (missingScope(e)) return json({ error: SCOPE_NOT_APPLIED }, 503)
    throw e
  }
  return json({ error: 'method not allowed' }, 405)
}
