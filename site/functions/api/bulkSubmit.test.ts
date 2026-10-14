// The bulk bar's real submission, end to end against the handlers over a local D1 (`sqliteD1`): a large set
// goes in batches, and lands as exactly the prefixes sent — staging as ONE batch (merged), the ledger as one
// row per prefix. (Never against a deployment's D1: dev shares prod's.)
import { describe, expect, it } from 'vitest'
import { onRequest as plansRoute } from './plans/[[path]]'
import { sqliteD1 } from '../_lib/testD1'
import { HttpError, stageMany } from '../../src/batches'
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

})

// A filtered row's (or the bulk bar's) action end to end: the click resolves the matches, a large set asks,
// the confirm sends them in batches (progress per batch), and the accepted state is what landed — over the
// real handlers and a local D1.
describe('a click on the matches → confirm → batches → accepted, against a local D1', () => {
  /** A controller sending through the routes; every state it passes through, as a line. */
  async function harness(items: CoverItem[]) {
    const { db, raw } = await sqliteD1('cw')
    raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json) VALUES ('2026-10-09', 'path', 2, '[]')`)
    const env = { ...base, DB: db }
    const sent: string[] = []
    const ok = async (r: Response) => { const j = await r.json(); if (!r.ok) throw new HttpError(JSON.stringify(j), r.status); return j }
    const deps: ActDeps = {
      scheme: 'gs://',
      resolve: async () => ({ items, complete: true }),
      asOf: () => '2026-10-09',
      assign: async () => { throw new Error('m3 keeps no ownership ledger') },
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
    const h = await harness(dirs)
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
})
