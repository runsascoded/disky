import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { shared, snapshotsPrefix } from './shared'

describe('shared', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('one computation per key, shared by concurrent callers', async () => {
    const map = new Map<string, Promise<number>>()
    let calls = 0
    const make = async () => { calls++; return 7 }
    const [a, b] = await Promise.all([shared(map, 'k', make, 1000), shared(map, 'k', make, 1000)])
    expect([a, b, calls]).toEqual([7, 7, 1])
    expect(await shared(map, 'k', make, 1000)).toBe(7)
    expect(calls).toBe(1)
  })

  it('a rejected computation is evicted, so the next caller retries', async () => {
    const map = new Map<string, Promise<number>>()
    let calls = 0
    const make = async () => { if (++calls === 1) throw new Error('boom'); return 9 }
    await expect(shared(map, 'k', make, 1000)).rejects.toThrow('boom')
    expect(await shared(map, 'k', make, 1000)).toBe(9)
    expect(calls).toBe(2)
  })

  it('a caller stuck on an entry that never settles evicts it and recomputes in its own context', async () => {
    const map = new Map<string, Promise<number>>()
    const frozen = new Promise<number>(() => {}) // a cancelled request's promise
    map.set('k', frozen)
    let calls = 0
    const p = shared(map, 'k', async () => { calls++; return 3 }, 500)
    await vi.advanceTimersByTimeAsync(499)
    expect(calls).toBe(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(await p).toBe(3)
    expect(calls).toBe(1)
    expect(map.get('k')).not.toBe(frozen)
  })

  it('a second stall is the caller\'s failure', async () => {
    const map = new Map<string, Promise<number>>()
    map.set('k', new Promise<number>(() => {}))
    const p = shared(map, 'k', () => new Promise<number>(() => {}), 100)
    const settled = p.then(() => 'ok', e => (e as Error).message)
    await vi.advanceTimersByTimeAsync(200)
    expect(await settled).toBe('shared k: no result after 100 ms')
    expect(map.has('k')).toBe(false)
  })
})

describe('snapshotsPrefix', () => {
  it('is the bare snapshots dir for the default store and a named subdir otherwise', () => {
    expect(snapshotsPrefix({})).toBe('snapshots/')
    expect(snapshotsPrefix({ SNAPSHOTS_SUBDIR: '' })).toBe('snapshots/')
    expect(snapshotsPrefix({ SNAPSHOTS_SUBDIR: 'cw' })).toBe('snapshots/cw/')
    expect(snapshotsPrefix({ SNAPSHOTS_SUBDIR: '/cw/' })).toBe('snapshots/cw/')
  })
})
