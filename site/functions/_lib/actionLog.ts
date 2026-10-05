/** The ownership ledger as an audit log: every owner action, newest first,
 * with what became of it — the `/assignments` page's actions table and
 * `GET /api/actions?log=1`. */
import type { Env } from './auth.js'
import type { ActionLogRow } from './actionLogShape.js'

export type { ActionLogRow, ActionStatus } from './actionLogShape.js'

export const LOG_MAX = 500

// `substr(…) = prefix`, never LIKE: `_` (common in these paths) is a LIKE wildcard.
const LOG_SQL = `
SELECT a.id, a.ts, a.actor AS who, a.pattern AS prefix, a.owner, a.memo, o.tombstoned AS retracted,
  CASE
    WHEN o.tombstoned IS NOT NULL THEN 'retracted'
    WHEN EXISTS (SELECT 1 FROM owner_prefixes n WHERE n.prefix = o.prefix AND n.tombstoned IS NULL
                 AND (n.ts > o.ts OR (n.ts = o.ts AND n.action_id > o.action_id))) THEN 'superseded'
    WHEN EXISTS (SELECT 1 FROM owner_prefixes n WHERE n.prefix <> o.prefix AND n.tombstoned IS NULL
                 AND substr(o.prefix, 1, length(n.prefix)) = n.prefix
                 AND (n.ts > o.ts OR (n.ts = o.ts AND n.action_id > o.action_id))) THEN 'overridden'
    ELSE 'live'
  END AS status
FROM actions a JOIN owner_prefixes o ON o.action_id = a.id
WHERE a.set_owner = 1
ORDER BY a.id DESC
LIMIT ? OFFSET ?`

export async function actionLog(env: Env, limit: number, offset: number): Promise<{ total: number; rows: ActionLogRow[] }> {
  const db = env.DB!
  const [total, rows] = await Promise.all([
    db.prepare('SELECT COUNT(*) AS n FROM actions WHERE set_owner = 1').first<{ n: number }>(),
    db.prepare(LOG_SQL).bind(Math.min(Math.max(1, limit), LOG_MAX), Math.max(0, offset)).all<ActionLogRow>(),
  ])
  return { total: total?.n ?? 0, rows: rows.results }
}
