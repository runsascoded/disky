/**
 * The deployment's `allowed_emails` table (`email, note, who, ts, read_only`; edited at
 * /admin/db) as `@open-athena/auth`'s `AllowlistStore`, so `authRoutes` can
 * put a share link's recipient on it (`POST /api/auth/grants` with
 * `allowlist: true`, specs/mint-link-allowlist.md).
 *
 * The table has no scopes column, just `read_only`: a row admits its email at
 * the base scope, or the read-only tier when set (`scopesFor`). So a full row
 * reads as the base scope plus its read-only half (it covers a read-only
 * link), a read-only row as the read-only tier alone, and a `put` sets
 * `read_only` from whether the scopes include the base one. No `source`
 * column either, so every row is `manual` and a directory sync
 * (`replaceSource`) is refused.
 */
import type { AllowEntry, AllowlistStore } from '@open-athena/auth'
import { allowedRow, baseReadScope, baseScope, type Env } from './auth.js'

interface Row { email: string; note: string | null; who: string; ts: number; read_only: number }

export function siteAllowlist(env: Env): AllowlistStore {
  const db = env.DB!
  const base = baseScope(env)
  const scopesOf = (readOnly: number): string[] => (readOnly ? [baseReadScope(env)] : [base, baseReadScope(env)])
  const entry = (r: Row): AllowEntry => ({ email: r.email, scopes: scopesOf(r.read_only), source: 'manual', note: r.note, addedBy: r.who, updatedAt: r.ts })
  return {
    async lookup(email) {
      const row = await allowedRow(db, email.toLowerCase())
      return row ? scopesOf(row.read_only) : null
    },
    async list() {
      const { results } = await db.prepare('SELECT email, note, who, ts, read_only FROM allowed_emails ORDER BY email').all<Row>()
      return results.map(entry)
    },
    async put(e) {
      await db.prepare(
        `INSERT INTO allowed_emails (email, note, who, ts, read_only) VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(email) DO UPDATE SET note = excluded.note, who = excluded.who, ts = excluded.ts, read_only = excluded.read_only`,
      ).bind(e.email.toLowerCase(), e.note, e.addedBy ?? 'share link', e.updatedAt, e.scopes.includes(base) ? 0 : 1).run()
    },
    async remove(email) {
      const res = await db.prepare('DELETE FROM allowed_emails WHERE email = ?').bind(email.toLowerCase()).run()
      return (res.meta?.changes ?? 0) > 0
    },
    async replaceSource() {
      throw new Error('allowed_emails has no `source` column: directory sync is not supported here')
    },
  }
}
