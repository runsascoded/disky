/** The Slack digest's live format settings (specs/digest-runway-marks.md §4; D1 `digest_settings`, migration
 * `digest-settings`): what `/admin/slack` picks, and what the job renders under over its config file's values
 * (`digest.live_settings`).
 *
 *   GET /api/digest-settings?template=quota → {settings: {template, params, quota_marks, updated_by, updated_at} | null}
 *   PUT /api/digest-settings {template, params, quota_marks} → {settings}                                        admin
 *
 * GET is viewer-gated (the job reads it with its grant; settings hold no secrets). `params` is the OP's
 * `src/digest/codec` string, read strictly (`parseParams`); `quota_marks` the replies' marks. Each PUT logs its old
 * and new row to `admin_edits`. A D1 without the table reads as no row (the job keeps its file's values). A template's
 * old name (`cw`, `gcs`: `TEMPLATE_ALIASES`) reads and writes its name's row. */
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, json, requireAdmin, requireViewer } from '../_lib/auth.js'
import { parseParams } from '../../src/digest/codec.js'
import { canonicalTemplate, TEMPLATE_ALIASES, TEMPLATES } from '../../src/digest/templates.js'

export const TABLE = 'digest_settings'
/** `digest.QUOTA_MARKS`: keep identical. */
export const QUOTA_MARKS = ['warning', 'circles', 'runway', 'runway-hue', 'arrow-chip']

export interface DigestSettings { template: string; params: string; quota_marks: string; updated_by: string; updated_at: number }

const missingTable = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

/** The template's old name, whose row a database the `digest-template-rename` recipe hasn't reached still holds. */
const aliasOf = (template: string): string => Object.keys(TEMPLATE_ALIASES).find(a => TEMPLATE_ALIASES[a] === template) ?? template

/** `template`'s row (a canonical name). The transition read: a row under its old name stands in until the recipe
 *  `digest-template-rename` re-keys it, the newer of the two where both are (as the recipe keeps); drop the alias
 *  half once every deployment's D1 has the recipe. */
export async function getSettings(db: D1Database, template: string): Promise<DigestSettings | null> {
  try {
    const row = await db.prepare(`SELECT template, params, quota_marks, updated_by, updated_at FROM ${TABLE} WHERE template IN (?, ?)
                                  ORDER BY updated_at DESC, template = ? DESC LIMIT 1`)
      .bind(template, aliasOf(template), template).first<DigestSettings>()
    return row && { ...row, template }
  } catch (e) {
    if (missingTable(e)) return null
    throw e
  }
}

/** The PUT body, or why it's refused. */
export function parseSettings(b: unknown): { template: string; params: string; quota_marks: string } | string {
  if (typeof b !== 'object' || b === null || Array.isArray(b)) return 'body: a JSON object'
  const { params, quota_marks } = b as Record<string, unknown>
  const template = canonicalTemplate((b as Record<string, unknown>).template)
  if (!template) return `template: ${TEMPLATES.join(' | ')}`
  if (typeof quota_marks !== 'string' || !QUOTA_MARKS.includes(quota_marks)) return `quota_marks: ${QUOTA_MARKS.join(' | ')}`
  if (typeof params !== 'string') return 'params: a string'
  try {
    parseParams(params)
  } catch (e) {
    return `params: ${(e as Error).message}`
  }
  return { template, params, quota_marks }
}

export async function putSettings(db: D1Database, s: { template: string; params: string; quota_marks: string }, who: string, ts: number): Promise<DigestSettings> {
  const old = await getSettings(db, s.template)
  const row: DigestSettings = { ...s, updated_by: who, updated_at: ts }
  await db.batch([
    // the transition write: the row lands under the name, and its old name's row (read in its place until now) goes
    db.prepare(`DELETE FROM ${TABLE} WHERE template = ? AND template != ?`).bind(aliasOf(row.template), row.template),
    db.prepare(`INSERT INTO ${TABLE} (template, params, quota_marks, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (template) DO UPDATE SET params = excluded.params, quota_marks = excluded.quota_marks,
                  updated_by = excluded.updated_by, updated_at = excluded.updated_at`)
      .bind(row.template, row.params, row.quota_marks, row.updated_by, row.updated_at),
    db.prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)')
      .bind(TABLE, row.template, old ? 'update' : 'insert', who, ts, old ? JSON.stringify(old) : null, JSON.stringify(row)),
  ])
  return row
}

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const template = canonicalTemplate(new URL(ctx.request.url).searchParams.get('template'))
  if (!template) return json({ error: `template: ${TEMPLATES.join(' | ')}` }, 400)
  if (!ctx.env.DB) return json({ settings: null }, 200, { 'cache-control': 'no-store' })
  return json({ settings: await getSettings(ctx.env.DB, template) }, 200, { 'cache-control': 'no-store' })
}

export const onRequestPut = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const s = parseSettings(await ctx.request.json().catch(() => null))
  if (typeof s === 'string') return json({ error: s }, 400)
  if (!ctx.env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  const who = id.email ?? id.name ?? 'admin'
  return json({ settings: await putSettings(ctx.env.DB, s, who, Math.floor(Date.now() / 1000)) }, 200, { 'cache-control': 'no-store' })
}
