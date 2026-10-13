import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'
import { canonIn } from './identityRegistry'
import { ledgerKey, ledgerKeyer, ownerScope, ownerScopeReady, scopeKeyOf } from './viewScope'

const REG = { 'grace-hopper': { u: 'grace-hopper', name: 'Grace' }, grace: { u: 'grace-hopper', name: 'Grace' } }
const EMPTY = {}

describe('ownerScope: the owner axis as request params', () => {
  const scope = (oP: string | undefined, o: { byP?: string; myUser?: string | null; reg?: typeof REG | typeof EMPTY } = {}) => {
    const { qs, ownerMode, ledgered, meUnmapped } = ownerScope({
      ownersMode: true, oP, byP: o.byP, myUser: o.myUser ?? null, canon: w => canonIn(o.reg ?? REG, w),
    })
    return { qs, ownerMode, ledgered, meUnmapped }
  }
  it('pools, people, exclusions and the unscoped view', () => {
    expect([undefined, 'unowned', 'owned', 'grace', '!grace,bob', 'me'].map(o => scope(o, { myUser: 'bob' }))).toEqual([
      { qs: '', ownerMode: 'all', ledgered: false, meUnmapped: false },
      { qs: '&o=unowned', ownerMode: 'unowned', ledgered: true, meUnmapped: false },
      { qs: '&o=owned', ownerMode: 'owned', ledgered: true, meUnmapped: false },
      { qs: '&lens=user:grace-hopper', ownerMode: 'user', ledgered: true, meUnmapped: false },
      { qs: '&o=!grace-hopper,bob', ownerMode: 'others', ledgered: true, meUnmapped: false },
      { qs: '&lens=user:bob', ownerMode: 'user', ledgered: true, meUnmapped: false },
    ])
  })
  it('a golfed key reads as itself until the registry loads — why a user view waits for it', () => {
    expect([scope('grace', { reg: EMPTY }).qs, scope('grace').qs]).toEqual(['&lens=user:grace', '&lens=user:grace-hopper'])
  })
  it('`?o=me` with no id yet is the unscoped view — why it waits for the id', () => {
    expect(scope('me')).toEqual({ qs: '', ownerMode: 'all', ledgered: false, meUnmapped: true })
  })
  it('`?by=` rides a user lens only', () => {
    expect([scope('grace', { byP: 'grace' }).qs, scope('unowned', { byP: 'grace' }).qs])
      .toEqual(['&lens=user:grace-hopper&by=grace-hopper', '&o=unowned'])
  })
})

describe('ownerScopeReady', () => {
  const ready = (oP: string | undefined, registryReady: boolean, myUserReady: boolean, byP?: string) =>
    ownerScopeReady({ ownersMode: true, oP, byP, registryReady, myUserReady })
  it('pools and the unscoped view never wait; people wait for the registry, `me` for the id', () => {
    expect([
      ready(undefined, false, false), ready('unowned', false, false), ready('owned', false, false),
      ready('grace', false, true), ready('grace', true, false), ready('!grace', false, true),
      ready('me', true, false), ready('me', false, true), ready('me', false, true, 'grace'), ready('me', true, true, 'grace'),
    ]).toEqual([
      true, true, true,
      false, true, false,
      false, true, false, true,
    ])
  })
  it('a store without the owner axis never waits', () => {
    expect(ownerScopeReady({ ownersMode: false, oP: 'grace', byP: undefined, registryReady: false, myUserReady: false })).toBe(true)
  })
})

describe('ledgerKey', () => {
  it("is '0' until the ledger moves past its first-loaded revision", () => {
    expect([ledgerKey('0.0', undefined), ledgerKey('3.42', '3.42'), ledgerKey('4.43', '3.42')]).toEqual(['0', '0', '4.43'])
  })
  it('records the first revision seen once loaded, then follows changes', () => {
    const k = ledgerKeyer()
    expect([k('0.0', false), k('3.42', true), k('3.42', true), k('4.43', true), k('3.42', true)]).toEqual(['0', '0', '0', '4.43', '0'])
  })
})

// A cold `?o=…` page load, as TSQ sees it: one observer per view query (the map's subtree + its depth-1
// companion, the diff's depth-1, full walk and summary), each re-optioned on every "render" as the async
// inputs land — registry, viewer id, ledger — in the order a cold load meets them. `fetch` records each
// request and whether it was aborted. The keys and URLs follow `App.tsx`'s shape around the scope parts.
interface Inputs { oP: string | undefined; registry: boolean; myUser: string | null; myUserReady: boolean; ledger: string | null }
type Ledgerer = (rev: string, loaded: boolean) => string

async function coldLoad(timeline: Inputs[], ledgerer: Ledgerer = ledgerKeyer(), ungated = false) {
  const requests: { url: string; aborted: boolean }[] = []
  const pending: (() => void)[] = []
  const fetchMock = (url: string, signal?: AbortSignal) => new Promise<string>((resolve, reject) => {
    const req = { url, aborted: false }
    requests.push(req)
    signal?.addEventListener('abort', () => { req.aborted = true; reject(new DOMException('aborted', 'AbortError')) })
    pending.push(() => resolve(url))
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const views: [string, string][] = [
    ['subtree', '/api/subtree?date=261007&path='],
    ['subtree-d1', '/api/subtree?date=261007&path=&depth=1'],
    ['diff-l1', '/api/diff?from=261006&to=261007&path=&depth=1'],
    ['diff', '/api/diff?from=261006&to=261007&path='],
    ['diff-sum', '/api/diff?from=261006&to=261007&path=&summary=1'],
  ]
  const options = (s: Inputs) => {
    const owner = ownerScope({ ownersMode: true, oP: s.oP, byP: undefined, myUser: s.myUser, canon: w => canonIn(s.registry ? REG : EMPTY, w) })
    const ready = ungated || ownerScopeReady({ ownersMode: true, oP: s.oP, byP: undefined, registryReady: s.registry, myUserReady: s.myUserReady })
    const scopeKey = scopeKeyOf(owner.qs, owner.ledgered, ledgerer(s.ledger ?? '0.0', s.ledger !== null))
    return views.map(([name, base]) => {
      const url = base.replace(/(&depth=1|&summary=1)?$/, `${owner.qs}$1`)
      return {
        queryKey: [name, scopeKey],
        enabled: ready,
        queryFn: ({ signal }: { signal: AbortSignal }) => fetchMock(url, signal),
      }
    })
  }
  const observers = options(timeline[0]).map(o => new QueryObserver(qc, o))
  const unsubs = observers.map(o => o.subscribe(() => {}))
  for (const s of timeline.slice(1)) options(s).forEach((o, i) => observers[i].setOptions(o))
  pending.forEach(f => f())
  await new Promise(r => setTimeout(r, 0))
  unsubs.forEach(u => u())
  return requests.map(r => [r.url, r.aborted] as const)
}

const cold = (oP: string | undefined): Inputs => ({ oP, registry: false, myUser: null, myUserReady: false, ledger: null })

const BATCH = (qs: string) => [
  `/api/subtree?date=261007&path=${qs}`,
  `/api/subtree?date=261007&path=${qs}&depth=1`,
  `/api/diff?from=261006&to=261007&path=${qs}&depth=1`,
  `/api/diff?from=261006&to=261007&path=${qs}`,
  `/api/diff?from=261006&to=261007&path=${qs}&summary=1`,
]
const settled = (qs: string) => BATCH(qs).map(u => [u, false])
const aborted = (qs: string) => BATCH(qs).map(u => [u, true])

describe('a cold owner-axis load sends each view request once, none aborted', () => {
  it('the unowned pool: the ledger landing after the first batch keeps its key', async () => {
    expect(await coldLoad([
      cold('unowned'),
      { ...cold('unowned'), registry: true },
      { ...cold('unowned'), registry: true, ledger: '3.42' },
    ])).toEqual(settled('&o=unowned'))
  })
  it('a golfed user key: nothing fires until the registry names the user', async () => {
    expect(await coldLoad([
      cold('grace'),
      { ...cold('grace'), ledger: '3.42' },
      { ...cold('grace'), ledger: '3.42', registry: true },
    ])).toEqual(settled('&lens=user:grace-hopper'))
  })
  it('`?o=me`: nothing fires until the viewer id lands (no unscoped batch first)', async () => {
    expect(await coldLoad([
      cold('me'),
      { ...cold('me'), registry: true },
      { ...cold('me'), registry: true, myUser: 'bob', myUserReady: true },
      { ...cold('me'), registry: true, myUser: 'bob', myUserReady: true, ledger: '3.42' },
    ])).toEqual(settled('&lens=user:bob'))
  })
  it('the plain view fires on the first render, waiting on nothing', async () => {
    expect(await coldLoad([cold(undefined)])).toEqual(settled(''))
  })
  it('a later ledger change re-reads (the in-flight batch for the old revision is dropped)', async () => {
    expect(await coldLoad([
      { ...cold('unowned'), ledger: '3.42' },
      { ...cold('unowned'), ledger: '4.43' },
    ])).toEqual([...aborted('&o=unowned'), ...settled('&o=unowned')])
  })
  it('keying on the raw revision (the bug) aborts the first batch and repeats it', async () => {
    expect(await coldLoad([cold('unowned'), { ...cold('unowned'), ledger: '3.42' }], rev => rev))
      .toEqual([...aborted('&o=unowned'), ...settled('&o=unowned')])
  })
  it('not waiting for the registry (the bug, for a golfed key) sends the wrong user, then the right one', async () => {
    const early: Inputs = { ...cold('grace'), registry: false }
    // `registryReady` forced: the pre-fix gate (none) over the same timeline.
    expect(await coldLoad([early, { ...early, registry: true }], undefined, true))
      .toEqual([...aborted('&lens=user:grace'), ...settled('&lens=user:grace-hopper')])
  })
})
