import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { inFlight, JOIN, joinBound, Req, resetJoinStats, shared, snapshotsPrefix, track } from './shared'

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

describe('inFlight', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('shares a computation while it runs, and drops it once settled', async () => {
    const map = new Map<string, Promise<number>>()
    let calls = 0
    const make = async () => { calls++; return 4 }
    expect(await Promise.all([inFlight(map, 'k', make, { name: 'm' }), inFlight(map, 'k', make, { name: 'm' })])).toEqual([4, 4])
    expect([calls, map.size]).toEqual([1, 0])
  })

  it('a joiner stuck on a frozen (untracked) entry evicts it after its bound and computes, unbounded, itself', async () => {
    const join0 = { ...JOIN }
    Object.assign(JOIN, { min: 100, max: 100 })
    const map = new Map<string, Promise<number>>()
    const frozen = new Promise<number>(() => {})
    map.set('k', frozen)
    let calls = 0
    const p = inFlight(map, 'k', () => new Promise<number>(r => { calls++; setTimeout(() => r(6), 10_000) }), { name: 'm' })
    Object.assign(JOIN, join0)
    await vi.advanceTimersByTimeAsync(99)
    expect(calls).toBe(0)
    await vi.advanceTimersByTimeAsync(10_001)
    expect([await p, calls, map.size]).toEqual([6, 1, 0])
  })

  it('a rejected entry rejects its joiners and leaves the map', async () => {
    const map = new Map<string, Promise<number>>()
    const make = () => Promise.reject(new Error('boom'))
    const [a, b] = await Promise.allSettled([inFlight(map, 'k', make, { name: 'm' }), inFlight(map, 'k', make, { name: 'm' })])
    expect([a, b].map(x => x.status === 'rejected' && (x.reason as Error).message)).toEqual(['boom', 'boom'])
    expect(map.size).toBe(0)
  })
})

// Another request's work, joined: each test's requests are `Req`s (what `withStore` puts on a request's env).
describe('join', () => {
  const join0 = { ...JOIN }
  beforeEach(() => { vi.useFakeTimers(); resetJoinStats(); Object.assign(JOIN, { k: 2, min: 100, max: 1000, cold: 500, budget: 2000 }) })
  afterEach(() => { vi.useRealTimers(); Object.assign(JOIN, join0) })
  const frozen = () => new Promise<number>(() => {})
  /** `owner`'s entry at `key` in `map`, as `shared` / `inFlight` leave one: tracked, never settling. */
  const hold = (map: Map<string, Promise<number>>, key: string, owner: Req, name = 'm') => {
    const p = frozen()
    map.set(key, p)
    track(p, { name, req: owner }, () => { if (map.get(key) === p) map.delete(key) })
    return p
  }
  const settledBy = async <T>(p: Promise<T>, ms: number) => {
    let done = false
    void p.then(() => { done = true }, () => { done = true })
    await vi.advanceTimersByTimeAsync(ms)
    return done
  }

  it('waits on a live entry\'s owner, never evicting it, and shares its result', async () => {
    const map = new Map<string, Promise<number>>()
    const a = new Req(), b = new Req()
    let calls = 0
    const make = () => new Promise<number>(r => { calls++; setTimeout(() => r(8), 300) })
    const pa = shared(map, 'k', make, 10_000, { name: 'm', req: a })
    const pb = shared(map, 'k', make, 10_000, { name: 'm', req: b })
    await vi.advanceTimersByTimeAsync(300)
    expect([await pa, await pb, calls, b.waited]).toEqual([8, 8, 1, 300])
  })

  it('a frozen entry is joined until it is `joinBound` old, then its owner is dead: every entry it owns is evicted', async () => {
    const m1 = new Map<string, Promise<number>>(), m2 = new Map<string, Promise<number>>()
    const a = new Req(), b = new Req(), c = new Req()
    hold(m1, 'k', a)
    hold(m2, 'k', a)
    let calls = 0
    const make = async () => { calls++; return 3 }
    const pb = shared(m1, 'k', make, 10_000, { name: 'm', req: b })
    expect(await settledBy(pb, 499)).toBe(false)
    expect(await settledBy(pb, 1)).toBe(true)
    // The other memo's entry went with its owner: a later request computes at once, no wait.
    const pc = inFlight(m2, 'k', make, { name: 'm', req: c })
    expect([await pb, await pc, calls, a.dead, b.waited, c.waited, m2.size]).toEqual([3, 3, 2, true, 500, 0, 0])
  })

  it('an entry already past its bound convicts its owner at once', async () => {
    const map = new Map<string, Promise<number>>()
    const a = new Req(), b = new Req()
    hold(map, 'k', a)
    await vi.advanceTimersByTimeAsync(5000) // the dead request's entry sat untouched
    const pb = shared(map, 'k', async () => 4, 10_000, { name: 'm', req: b })
    expect(await settledBy(pb, 0)).toBe(true)
    expect([await pb, b.waited, a.dead]).toEqual([4, 0, true])
  })

  it('a cancelled request\'s entries are evicted at its cancel, and their joiners woken', async () => {
    const m1 = new Map<string, Promise<number>>(), m2 = new Map<string, Promise<number>>()
    const ctl = new AbortController()
    const a = new Req(ctl.signal), b = new Req()
    hold(m1, 'k', a)
    hold(m2, 'k', a)
    const pb = shared(m1, 'k', async () => 5, 10_000, { name: 'm', req: b })
    expect(await settledBy(pb, 50)).toBe(false)
    ctl.abort()
    expect(await settledBy(pb, 0)).toBe(true)
    expect([await pb, b.waited, m1.size, m2.size, a.dead]).toEqual([5, 50, 1, 0, true])
  })

  it('a request already cancelled shares nothing', async () => {
    const map = new Map<string, Promise<number>>()
    const ctl = new AbortController()
    ctl.abort()
    const p = inFlight(map, 'k', frozen, { name: 'm', req: new Req(ctl.signal) })
    expect([map.size, await settledBy(p, 0)]).toEqual([0, false])
  })

  it('a request\'s waits on others\' entries share one budget, across memos; past it an entry is evicted alone', async () => {
    Object.assign(JOIN, { budget: 550 })
    const ms = [0, 1, 2].map(() => new Map<string, Promise<number>>())
    const owners = [new Req(), new Req(), new Req()]
    const b = new Req()
    const t0 = Date.now()
    const got: number[] = []
    for (const [i, m] of ms.entries()) {
      hold(m, 'k', owners[i]) // each entry young when joined
      const p = shared(m, 'k', async () => 6, 10_000, { name: 'm', req: b })
      await vi.advanceTimersByTimeAsync(500)
      got.push(await p)
    }
    // 500 (the cold bound: owner convicted) + 50 (the budget's rest, short of the bound — 100 now that `m`s settle at
    // once: evicted alone) + 0 (none left: evicted alone)
    expect([got, b.waited, owners.map(o => o.dead), ms.map(m => m.size)]).toEqual([[6, 6, 6], 550, [true, false, false], [1, 1, 1]])
    expect(Date.now() - t0).toBe(1500)
  })

  it('an evicted promise never returns to its memo, settling or not', async () => {
    const map = new Map<string, Promise<number>>()
    const a = new Req(), b = new Req()
    let settle!: (v: number) => void
    const pa = shared(map, 'k', () => new Promise<number>(r => { settle = r }), 10_000, { name: 'm', req: a })
    const pb = shared(map, 'k', async () => 2, 10_000, { name: 'm', req: b })
    await vi.advanceTimersByTimeAsync(500)
    const held = map.get('k')
    settle(1)
    expect([await pa, await pb, map.get('k') === held, await map.get('k')]).toEqual([1, 2, true, 2])
  })

  it('a request\'s own entry is its own work: waited on as such, not joined', async () => {
    const map = new Map<string, Promise<number>>()
    const a = new Req()
    const make = () => new Promise<number>(r => setTimeout(() => r(7), 3000))
    const p1 = shared(map, 'k', make, 10_000, { name: 'm', req: a })
    const p2 = shared(map, 'k', make, 10_000, { name: 'm', req: a })
    await vi.advanceTimersByTimeAsync(3000)
    expect([await p1, await p2, a.dead, a.waited]).toEqual([7, 7, false, 0])
  })

  it('a kind\'s bound follows what it takes to settle', async () => {
    expect(joinBound('m')).toBe(500) // cold
    const map = new Map<string, Promise<number>>()
    const p = inFlight(map, 'k', () => new Promise<number>(r => setTimeout(() => r(1), 200)), { name: 'm', req: new Req() })
    await vi.advanceTimersByTimeAsync(200)
    await p
    expect([joinBound('m'), joinBound('other')]).toEqual([400, 500])
  })

  it('traces each wait as `join-<memo>`, and a conviction as `join-evict`', async () => {
    const map = new Map<string, Promise<number>>()
    const a = new Req(), b = new Req()
    const t: [string, number, string?][] = []
    b.trace = (name, ms, desc) => t.push(desc ? [name, ms, desc] : [name, ms])
    hold(map, 'k', a, 'handle')
    const p = shared(map, 'k', async () => 1, 10_000, { name: 'handle', req: b })
    await vi.advanceTimersByTimeAsync(500)
    expect([await p, t]).toEqual([1, [['join-handle', 500], ['join-evict', 1, 'handle']]])
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
