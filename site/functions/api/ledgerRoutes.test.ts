// The ownership reads exist only where the deployment keeps a ledger
// (`hasLedger`): on cw's D1 they are a 404, not a 500 from a missing table —
// the same rule as `/api/actions`.
import { describe, expect, it } from 'vitest'
import { onRequestGet as assignments } from './assignments'
import { onRequestGet as estate } from './estate'
import { onRequestGet as owners } from './owners'
import { sqliteD1 } from '../_lib/testD1'

describe('ownership reads without a ledger', () => {
  it('/api/assignments, /api/estate and /api/owners are a 404 on the cw lineage', async () => {
    const { db } = await sqliteD1('cw')
    const env = { DB: db }
    const at = (path: string) => ({ request: new Request(`http://localhost${path}`), env }) as never
    const answer = async (r: Response) => [r.status, await r.json()]
    expect(await Promise.all([
      assignments(at('/api/assignments?date=2026-10-07')).then(answer),
      estate(at('/api/estate?date=2026-10-07&user=ann')).then(answer),
      owners(at('/api/owners?date=2026-10-07')).then(answer),
    ])).toEqual(Array(3).fill([404, { error: 'no ownership ledger on this deployment' }]))
  })
})
