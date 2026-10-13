// The bulk bar's real submission, end to end against the handlers over a local D1 (`sqliteD1`): a large set
// goes in batches, and lands as exactly the prefixes sent — staging as ONE batch (merged), the ledger as one
// row per prefix. (Never against a deployment's D1: dev shares prod's.)
import { describe, expect, it } from 'vitest'
import { onRequest as actionsRoute } from './actions'
import { onRequest as plansRoute } from './plans/[[path]]'
import { GCS_LEDGER, sqliteD1 } from '../_lib/testD1'
import { assignInBatches, HttpError, stageMany } from '../../src/batches'
import { actController, type ActDeps, type ActState } from '../../src/matchAct'
import type { CoverItem } from '../_lib/cover'

const B = 'bkt'
const base = { STAGING: '1', STORE_SCHEME: 'gs://', STORE_BUCKETS: B, DEV_EMAIL: 'ann@example.test' }

describe('bulk submission against a local D1', () => {
  it('stages 1,100 matching folders in 3 POSTs, merged into one batch', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-10-09', 'path', 2, '[]')`)
    const env = { ...base, DB: db }
    const prefixes = Array.from({ length: 1100 }, (_, i) => `gs://${B}/d${String(i).padStart(4, '0')}/`)
    const sent: string[] = []
    const post = (async (url: string, method: string, body: unknown) => {
      sent.push(url)
      const r = await plansRoute({ request: new Request(`http://localhost${url}`, { method, body: JSON.stringify(body) }), env } as never)
      const j = await r.json()
      if (!r.ok) throw new Error(JSON.stringify(j))
      return j
    }) as never
    const res = await stageMany(prefixes, 'tomat matches', undefined, post)
    const staged = await (await plansRoute({ request: new Request('http://localhost/api/plans/staged'), env } as never)).json() as { items: { prefix: string; batch_id: number }[]; batches: { id: number }[] }
    expect([
      sent,
      [res.plan_id, res.batch_id, res.staged.length],
      staged.batches.map(b => b.id),
      [...new Set(staged.items.map(i => i.batch_id))],
      staged.items.map(i => i.prefix).sort(),
    ]).toEqual([
      ['/api/plans/stage', '/api/plans/stage', '/api/plans/stage', '/api/plans/1/batches/merge'],
      [1, 1, 1100],
      [1],
      [1],
      [...prefixes].sort(),
    ])
  })

  it('assigns 1,100 folder prefixes in 3 POSTs (the API takes ≤ 500), one ledger row each', async () => {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(GCS_LEDGER)
    const env = { ...base, DB: db }
    const prefixes = Array.from({ length: 1100 }, (_, i) => `gs://${B}/d${String(i).padStart(4, '0')}/`)
    const statuses: number[] = []
    const n = await assignInBatches(prefixes, pattern => ({ pattern, owner: 'ann', memo: "bulk filter:'tomat'", scan: '2026-10-09' }), async actions => {
      const r = await actionsRoute({ request: new Request('http://localhost/api/actions', { method: 'POST', body: JSON.stringify(actions) }), env } as never)
      statuses.push(r.status)
      if (!r.ok) throw new Error(await r.text())
    })
    const rows = (await db.prepare('SELECT prefix, owner FROM owner_prefixes ORDER BY prefix').all<{ prefix: string; owner: string }>()).results
    expect([n, statuses, rows.length, rows.map(r => r.prefix), [...new Set(rows.map(r => r.owner))]]).toEqual([1100, [200, 200, 200], 1100, prefixes, ['ann']])
  })
})

// A filtered row's (or the bulk bar's) action end to end: the click resolves the matches, a large set asks,
// the confirm sends them in batches (progress per batch), and the accepted state is what landed — over the
// real handlers and a local D1.
describe('a click on the matches → confirm → batches → accepted, against a local D1', () => {
  /** A controller sending through the routes; every state it passes through, as a line. */
  async function harness(items: CoverItem[], ledger: boolean) {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-10-09', 'path', 2, '[]')`)
    if (ledger) raw.exec(GCS_LEDGER)
    const env = { ...base, DB: db }
    const sent: string[] = []
    const ok = async (r: Response) => { const j = await r.json(); if (!r.ok) throw new HttpError(JSON.stringify(j), r.status); return j }
    const deps: ActDeps = {
      scheme: 'gs://',
      resolve: async () => ({ items, complete: true }),
      asOf: () => '2026-10-09',
      assign: async actions => {
        sent.push(`POST /api/actions ${actions.length}`)
        return ok(await actionsRoute({ request: new Request('http://localhost/api/actions', { method: 'POST', body: JSON.stringify(actions) }), env } as never))
      },
      call: (async (url: string, method: string, body: unknown) => {
        sent.push(`${method} ${url}`)
        return ok(await plansRoute({ request: new Request(`http://localhost${url}`, { method, body: JSON.stringify(body) }), env } as never))
      }) as never,
    }
    let cur: ActState = { s: 'idle' }
    const states: string[] = []
    const ctl = actController(() => deps, s => {
      cur = s
      states.push(s.s === 'sending' ? `sending ${s.batch}/${s.batches}` : s.s === 'done' ? `done: ${s.text}${s.undo ? ' · undo' : ''}` : s.s)
    }, () => cur)
    return { ctl, states, sent, db }
  }
  const dirs = Array.from({ length: 1100 }, (_, i) => ({ path: `${B}/d${String(i).padStart(4, '0')}/tomat`, kind: 'dir' as const, b: 10, o: 1, roots: 1 }))

  it('stage 1,100 folders: asks, sends 3 POSTs + a merge, lands as one batch; undo takes them back', async () => {
    const h = await harness(dirs, false)
    await h.ctl.start({ kind: 'stage', memo: 'old tomat runs' })
    expect([h.states, h.sent]).toEqual([['resolving', 'confirm'], []])
    await h.ctl.confirm()
    const rows = async () => (await h.db.prepare('SELECT prefix, batch_id FROM plan_items ORDER BY prefix').all<{ prefix: string; batch_id: number }>()).results
    const notes = (await h.db.prepare('SELECT note FROM stage_batches').all<{ note: string }>()).results
    const landed = await rows()
    expect([h.states, h.sent, landed.map(r => r.prefix), [...new Set(landed.map(r => r.batch_id))], notes]).toEqual([
      ['resolving', 'confirm', 'sending 0/3', 'sending 1/3', 'sending 2/3', 'sending 3/3', 'done: staged 1,100 folders · undo'],
      ['POST /api/plans/stage', 'POST /api/plans/stage', 'POST /api/plans/stage', 'POST /api/plans/1/batches/merge'],
      dirs.map(d => `gs://${d.path}/`),
      [1],
      [{ note: 'old tomat runs' }],
    ])
    await h.ctl.undo()
    expect([h.states.slice(-2), h.sent.slice(4), await rows()]).toEqual([
      ['undoing', 'done: unstaged 1,100 items'],
      ['DELETE /api/plans/1/items', 'DELETE /api/plans/1/items', 'DELETE /api/plans/1/items'],
      [],
    ])
  })

  it('assign 120 lone files: asks (over the confirm threshold), one POST, one exact-object ledger row each', async () => {
    const files = Array.from({ length: 120 }, (_, i) => ({ path: `${B}/show/clip${String(i).padStart(3, '0')}.mp3`, kind: 'file' as const, b: 1, o: 1, roots: 1 }))
    const h = await harness(files, true)
    await h.ctl.start({ kind: 'assign', owner: 'ann', who: 'ann', memo: "bulk filter:'clip'" })
    await h.ctl.confirm()
    const rows = (await h.db.prepare('SELECT p.prefix, p.kind, p.owner, a.memo FROM owner_prefixes p JOIN actions a ON a.id = p.action_id ORDER BY p.prefix').all<{ prefix: string; kind: string | null; owner: string; memo: string }>()).results
    expect([h.states, h.sent, rows]).toEqual([
      ['resolving', 'confirm', 'sending 0/1', 'sending 1/1', 'done: assigned 120 files → ann'],
      ['POST /api/actions 120'],
      files.map(f => ({ prefix: `gs://${f.path}`, kind: 'object', owner: 'ann', memo: "bulk filter:'clip'" })),
    ])
  })
})
