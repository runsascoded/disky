import { describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { sqliteD1 } from './testD1'

// An isolate's interval-store state is held `IV_STATE_TTL` (plus the colo's copy): a scan another isolate already
// lists is re-checked past those caches once before it is a 404 (dev.cw.oa.dev, 2026-11-17T1202).
const reads: boolean[] = []
let stale: string[] = [], current: string[] = []
vi.mock('./index.js', async orig => ({
  ...await orig<typeof import('./index.js')>(),
  storeHeldScans: vi.fn(async (_env: unknown, fresh = false) => { reads.push(fresh); return fresh ? current : stale }),
}))
const { indexedScan } = await import('./scanArg')

describe('indexedScan over a stale held state', () => {
  const env = async (): Promise<Env> => ({ DB: (await sqliteD1()).db, PATH_STORE: 'intervals' }) as unknown as Env
  it.each([
    // [held state, fresh state, prefix, exact, answer, reads]
    ['a scan the held state lists: no re-read', ['2026-11-17T0602', '2026-11-17T1202'], ['2026-11-17T0602', '2026-11-17T1202'], '2026-11-17T1202', true, '2026-11-17T1202', [false]],
    ['a scan only the fresh state lists: found', ['2026-11-17T0602'], ['2026-11-17T0602', '2026-11-17T1202'], '2026-11-17T1202', true, '2026-11-17T1202', [false, true]],
    ['a scan neither lists: one re-read, then a miss', ['2026-11-17T0602'], ['2026-11-17T0602'], '2026-11-17T1802', true, null, [false, true]],
    ['a slug only the fresh state answers', ['2026-11-17T0602'], ['2026-11-17T0602', '2026-11-17T1202'], '2026-11-17T12', false, '2026-11-17T1202', [false, true]],
    ['a slug the held state answers: no re-read', ['2026-11-17T0602'], ['2026-11-17T0602', '2026-11-17T1202'], '2026-11-17T06', false, '2026-11-17T0602', [false]],
  ] as const)('%s', async (_name, held, fresh, prefix, exact, answer, want) => {
    stale = [...held]; current = [...fresh]; reads.length = 0
    expect([await indexedScan(await env(), prefix, exact), reads]).toEqual([answer, want])
  })
})
