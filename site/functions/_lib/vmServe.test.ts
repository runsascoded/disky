import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { fakeVmd, VM_ENV as ON } from './testVmd'
import type { VmAnswer, VmPins } from './vmAdapter'
import {
  beOf, beOfResponse, BeTrack, pinCookie, pinOf, resetVmd, stampBe, stampKind, VM_OFF_NOTE, vmDiffBody, VMD, type VmdStatus,
  vmKey, vmReady, vmShare, vmView, vmWhyNot,
} from './vmServe'

const req = (qs = '', cookie?: string, proto = 'https') => new Request(`${proto}://site.test/api/subtree${qs}`, cookie ? { headers: { cookie } } : {})

describe('the pin', () => {
  it('is `?be=`, else the cookie, else `idx`; always `idx` while VM mode is off', () => {
    expect([
      pinOf(req(), ON),
      pinOf(req('?be=vm'), ON),
      pinOf(req('', 'a=1; disky_be=vm'), ON),
      pinOf(req('?be=idx', 'disky_be=vm'), ON),
      pinOf(req('?be=x', 'disky_be=y'), ON),
      pinOf(req('?be=vm'), {}),
      pinOf(req('', 'disky_be=vm'), { VMD_URL: 'https://vmd.test', VM_TENANT: 't1' }),
    ]).toEqual([
      { pin: 'idx' },
      { pin: 'vm' },
      { pin: 'vm' },
      { pin: 'idx' },
      { pin: 'idx' },
      { pin: 'idx', note: VM_OFF_NOTE },
      { pin: 'idx' },
    ])
  })

  it('`VM_SHARE`: a probability, clamped; anything else none', () => {
    expect(['0.25', '2', '-1', 'x', '', undefined].map(VM_SHARE => vmShare({ ...ON, VM_SHARE }))).toEqual([0.25, 1, 0, 0, 0, 0])
  })

  it('the cookie: a `?be=` re-pins, a page view without a pin gets one by `VM_SHARE`, off clears a leftover', () => {
    const share = { ...ON, VM_SHARE: '0.25' }
    expect([
      pinCookie(req('?be=vm'), ON, false),
      pinCookie(req('?be=vm', 'disky_be=vm'), ON, true),
      pinCookie(req('?be=idx', 'disky_be=vm'), ON, false),
      pinCookie(req(), share, true, () => 0.1),
      pinCookie(req(), share, true, () => 0.5),
      pinCookie(req(), share, false, () => 0.1),
      pinCookie(req('', 'disky_be=idx'), share, true, () => 0.1),
      pinCookie(req('', '', 'http'), ON, true, () => 0),
      pinCookie(req('', 'disky_be=vm'), {}, true),
      pinCookie(req('', 'disky_be=vm'), {}, false),
      pinCookie(req('?be=vm'), {}, true),
    ]).toEqual([
      'disky_be=vm; Path=/; Max-Age=2592000; SameSite=Lax; Secure',
      null,
      'disky_be=idx; Path=/; Max-Age=2592000; SameSite=Lax; Secure',
      'disky_be=vm; Path=/; Max-Age=2592000; SameSite=Lax; Secure',
      'disky_be=idx; Path=/; Max-Age=2592000; SameSite=Lax; Secure',
      null,
      null,
      'disky_be=idx; Path=/; Max-Age=2592000; SameSite=Lax',
      'disky_be=; Path=/; Max-Age=0; SameSite=Lax; Secure',
      null,
      null,
    ])
  })
})

describe('what answered', () => {
  const said = (r: Response) => [r.headers.get('server-timing'), r.headers.get('x-disky-be-note')]

  it('a `vm` session starts as a fallback; `vmd` answering makes it `vm`; a store outside VM mode is `idx`', () => {
    const t = (cookie: string | undefined, f: (b: BeTrack) => void) => { const b = new BeTrack(req('', cookie), ON); f(b); return [b.answered, b.note] }
    expect([
      t(undefined, () => {}),
      t(undefined, b => b.fallback('why')),
      t('disky_be=vm', () => {}),
      t('disky_be=vm', b => b.fallback('why')),
      t('disky_be=vm', b => b.vm('lacks x')),
      t('disky_be=vm', b => b.outside('no tenant')),
      t(undefined, b => b.outside('no tenant')),
    ]).toEqual([
      ['idx', undefined],
      ['idx', undefined],
      ['idx-fallback', undefined],
      ['idx-fallback', 'why'],
      ['vm', 'lacks x'],
      ['idx', 'no tenant'],
      ['idx', undefined],
    ])
  })

  it('stamps one `be` timing entry and the note, ASCII; nothing while off but a note', () => {
    const r = stampBe(new Response('{}', { headers: { 'server-timing': 'auth;dur=3, be;desc="idx", total;dur=9' } }), { on: true, answered: 'vm', note: 'nodes — no mtime' })
    expect(said(r)).toEqual(['auth;dur=3, total;dur=9, be;desc="vm"', 'nodes ? no mtime'])
    expect(beOfResponse(r)).toEqual({ be: 'vm', note: 'nodes ? no mtime' })
    expect(said(stampBe(new Response('{}'), { on: false, answered: 'idx' }))).toEqual([null, null])
    expect(said(stampBe(new Response('{}'), { on: false, answered: 'idx', note: VM_OFF_NOTE }))).toEqual([null, VM_OFF_NOTE])
    // An immutable response is copied.
    const frozen = Response.redirect('https://site.test/x', 302)
    expect(said(stampBe(frozen, { on: true, answered: 'idx' }))).toEqual(['be;desc="idx"', null])
  })

  it('a composite: any fallback wins, else any `vm`; distinct notes joined', () => {
    const part = (answered: 'vm' | 'idx' | 'idx-fallback', note?: string) => stampBe(new Response('{}'), { on: true, answered, note })
    const vmReq = req('', 'disky_be=vm')
    expect([
      beOf(vmReq, ON, [part('vm', 'n1'), part('idx', 'store meta has no vmd tenant')]),
      beOf(vmReq, ON, [part('vm', 'n1'), part('idx-fallback', 'vmd 503: loading'), part('vm', 'n1')]),
      beOf(req(), ON, [part('idx'), part('idx')]),
      beOf(vmReq, ON, []),
      beOf(req('?be=vm'), {}, [new Response('{}')]),
    ]).toEqual([
      { on: true, answered: 'vm', note: 'n1; store meta has no vmd tenant' },
      { on: true, answered: 'idx-fallback', note: 'n1; vmd 503: loading' },
      { on: true, answered: 'idx' },
      { on: true, answered: 'idx-fallback' },
      { on: false, answered: 'idx', note: VM_OFF_NOTE },
    ])
  })

  it('which request kinds `vmd` takes', () => {
    expect([
      vmWhyNot({ primary: true }),
      vmWhyNot({ primary: false, store: 'meta' }),
      vmWhyNot({ primary: true, restricted: true }),
      vmWhyNot({ primary: true, query: {} }),
      vmWhyNot({ primary: true, owner: 'owned' }),
      vmWhyNot({ primary: true, by: 'u1' }),
      vmWhyNot({ primary: true, descent: true }),
    ]).toEqual([
      null,
      { why: 'store \'meta\' has no vmd tenant', outside: true },
      { why: 'path-restricted link: never sent to vmd' },
      { why: 'name filter (q=): vmd\'s answer has no match lists yet (specs/vm-mode.md 7.2)' },
      { why: 'owner scope (lens, o, by): vmd doesn\'t fold the live ledger yet (specs/vm-mode.md 2.1)' },
      { why: 'owner scope (lens, o, by): vmd doesn\'t fold the live ledger yet (specs/vm-mode.md 2.1)' },
      { why: 'delta map (dm=delta): vmd draws by max(before, after), the index reader\'s descent by change' },
    ])
  })

  it('the middleware stamps the map answers no route did', () => {
    const at = (path: string, cookie?: string) => new Request(`https://site.test${path}`, cookie ? { headers: { cookie } } : {})
    const s = (r: Response) => [r.headers.get('server-timing'), r.headers.get('x-disky-be-note')]
    expect([
      s(stampKind(new Response('{}'), at('/api/series?x=1', 'disky_be=vm'), ON)),
      s(stampKind(new Response('{}'), at('/api/name-summary', 'disky_be=vm'), ON)),
      s(stampKind(new Response('{}'), at('/api/series'), ON)),
      s(stampKind(new Response('{}', { headers: { 'server-timing': 'be;desc="vm"' } }), at('/api/subtree', 'disky_be=vm'), ON)),
      s(stampKind(new Response('{}'), at('/api/largest', 'disky_be=vm'), ON)),
      s(stampKind(new Response('{}'), at('/api/series', 'disky_be=vm'), {})),
    ]).toEqual([
      ['be;desc="idx-fallback"', 'size over time: vmd has no series yet'],
      ['be;desc="idx-fallback"', 'search summary: vmd has no name summary yet'],
      ['be;desc="idx"', null],
      ['be;desc="vm"', null],
      [null, null],
      [null, null],
    ])
  })
})

const PINS: VmPins = { gen: 'g1', manifest: 'm1.json' }
const NEW: VmPins = { gen: 'g1', manifest: 'm2.json' }

const hot = (pins = PINS, newest = '2026-11-02T0000'): VmdStatus => ({ tenant: 't1', state: 'hot', current: { pins, scans: 2, newest, classes: [1, 2, 3, 4] } })
const answer = (pins: VmPins): VmAnswer => ({
  v: 1, tenant: 't1', pins, scan: '2026-11-01T0000', path: '', cell: { share_log2: 4, atten_log2: 1 },
  total: { bytes: 10, objects: 1 }, nodes: 1, tree: { name: '', kind: 'dir', bytes: 10, objects: 1, children: [{ name: 'b', kind: 'dir', bytes: 10, objects: 1 }] },
  timing: { pass_ms: 1, merge_ms: 0, total_ms: 1.4, rows: 1, dirs: 1, shards: 1 },
})
const VIEW = { date: '2026-11-01T0000', path: '', w: 128, h: 128, minArea: 16 }

describe('the vmd client', () => {
  const real = VMD.fetch
  beforeEach(() => resetVmd())
  afterEach(() => { VMD.fetch = real })

  it('reads the status once per window; a loading box, a newer scan or a down box is a reason', async () => {
    const { calls } = fakeVmd({ status: () => hot() })
    expect([await vmReady(ON, ['2026-11-01T0000']), await vmReady(ON, ['2026-11-01T0000', '2026-11-03T0000'])]).toEqual([
      { pins: PINS },
      { why: 'scan 2026-11-03T0000 is newer than vmd\'s pack (newest 2026-11-02T0000)' },
    ])
    expect(calls).toEqual(['GET /v1/status'])
    resetVmd()
    fakeVmd({ status: () => ({ tenant: 't1', state: 'loading', current: null }) })
    expect(await vmReady(ON, [])).toEqual({ why: 'vmd is loading the tenant\'s pack' })
    resetVmd()
    fakeVmd({ status: () => ({ tenant: 't1', state: 'error', current: null, error: 'no pack' }) })
    expect(await vmReady(ON, [])).toEqual({ why: 'vmd has no pack for the tenant: no pack' })
    resetVmd()
    let n = 0
    VMD.fetch = async () => { n++; throw new Error('connect refused') }
    expect([await vmReady(ON, []), await vmReady(ON, [])]).toEqual([{ why: 'vmd unreachable: connect refused' }, { why: 'vmd unreachable: connect refused' }])
    expect(n).toEqual(1)
    resetVmd()
    fakeVmd({ status: () => hot(), unsigned: true })
    expect(await vmReady(ON, [])).toEqual({ why: 'vmd status: the reply\'s signature isn\'t the tenant key\'s' })
  })

  it('asks a view at the status\'s pins; a `409 pins` once more at the pins vmd holds', async () => {
    const { calls, views } = fakeVmd({
      status: () => hot(),
      view: b => {
        return (b.pins as VmPins).manifest === NEW.manifest ? { status: 200, body: answer(NEW) } : { status: 409, body: { error: 'pins', message: 'the loaded pack\'s pins differ', have: NEW } }
      },
    })
    const ready = await vmReady(ON, [VIEW.date])
    expect(ready).toEqual({ pins: PINS })
    expect(await vmView(ON, VIEW, PINS)).toEqual({ answer: answer(NEW) })
    expect(views.map(v => v.pins)).toEqual([PINS, NEW])
    // The status's pins follow.
    expect(await vmReady(ON, [VIEW.date])).toEqual({ pins: NEW })
    expect(calls).toEqual(['GET /v1/status', 'POST /v1/view', 'POST /v1/view'])
  })

  it('an error, a loading pack or an unsigned answer is a reason; the request names the cell rule', async () => {
    let reply: { status: number; body: unknown } = { status: 400, body: { error: 'bad-request', message: 'class 5: this store\'s classes are 1, 2, 3, 4' } }
    const { views: bodies } = fakeVmd({ view: () => reply })
    expect(await vmView(ON, { ...VIEW, classes: new Set(['5']) }, PINS)).toEqual({ why: 'vmd 400: bad-request: class 5: this store\'s classes are 1, 2, 3, 4' })
    expect(bodies).toEqual([{ pins: PINS, scan: VIEW.date, path: '', classes: [5], cell: { share_log2: 10, atten_log2: 1 }, deadline_ms: VMD.deadlineMs }])
    reply = { status: 503, body: { error: 'loading', message: 'a new pack is loading' } }
    expect(await vmView(ON, VIEW, PINS)).toEqual({ why: 'vmd 503: loading: a new pack is loading' })
    expect(await vmView(ON, { ...VIEW, atten: 3 }, PINS)).toEqual({ why: 'atten 3: VM mode takes a power of 2' })
    fakeVmd({ view: () => ({ status: 200, body: answer(PINS) }), unsigned: true })
    expect(await vmView(ON, VIEW, PINS)).toEqual({ why: 'vmd: the reply\'s signature isn\'t the tenant key\'s' })
  })

  it('a member read asks under the composite\'s floor', async () => {
    const { views: bodies } = fakeVmd({ view: () => ({ status: 200, body: answer(PINS) }) })
    await vmView(ON, { ...VIEW, floorBytes: 1000.5 }, PINS)
    expect(bodies).toEqual([{ pins: PINS, scan: VIEW.date, path: '', cell: { atten_log2: 1, floor_bytes: 1001, depth: 11 }, deadline_ms: VMD.deadlineMs }])
  })
})

describe('caching and bodies', () => {
  it('a `vm` key is the index reader\'s plus the backend and the pins', () => {
    expect(vmKey(new Request('https://subtree.cache/v14/d/?w=1&head=0'), PINS).url).toEqual('https://subtree.cache/v14/d/?w=1&head=0&be=vm&vv=1&vp=g1%2Fm1.json')
  })

  it('a diff body: rows cut as the index reader\'s, none for a summary', () => {
    const a: VmAnswer = {
      v: 1, tenant: 't1', pins: PINS, from: 'a', to: 'b', path: 'bk', cell: { share_log2: 4, atten_log2: 1 },
      total: { bytes: 300, objects: 3, bytes_to: 200, objects_to: 3 }, nodes: 3,
      tree: {
        name: 'bk', kind: 'dir', bytes: 300, objects: 3, bytes_to: 200, objects_to: 3,
        children: [
          { name: 'd', kind: 'dir', bytes: 200, objects: 2, bytes_to: 100, objects_to: 2, children: [{ name: 'x', kind: 'file', bytes: 200, objects: 1, bytes_to: 0, objects_to: 0 }, { name: '(other)', kind: 'dir', bytes: 0, objects: 1, bytes_to: 100, objects_to: 2, folded: 1 }] },
          { name: 'same', kind: 'file', bytes: 100, objects: 1, bytes_to: 100, objects_to: 1 },
        ],
      },
      timing: { pass_ms: 2, merge_ms: 0, total_ms: 2.6, rows: 3, dirs: 1, shards: 1 },
    }
    const row = (path: string, depth: number, kind: 'file' | 'dir', status: string, bb: number, ba: number, ob: number, oa: number, expanded = false) =>
      ({ path, depth, kind, status, bytes_before: bb, bytes_after: ba, objects_before: ob, objects_after: oa, ...(expanded ? { expanded: true } : {}) })
    const totals = { bytes_before: 300, bytes_after: 200, objects_before: 3, objects_after: 3 }
    const meta = { tier: 'vm', lookups: 0, lookups_capped: false, vm: { tenant: 't1', pins: PINS, ms: 3 } }
    expect(vmDiffBody(a, { from: 'a', to: 'b', path: 'bk', top: 500, summary: false })).toEqual({
      prev: 'a', curr: 'b', path: 'bk',
      rows: [row('d', 1, 'dir', 'changed', 200, 100, 2, 2, true), row('d/x', 2, 'file', 'removed', 200, 0, 1, 0), row('d/(other)', 2, 'dir', 'added', 0, 100, 1, 2)],
      ...totals, expansions: 1, truncated: false, threshold: 19, ...meta,
    })
    expect(vmDiffBody(a, { from: 'a', to: 'b', path: 'bk', top: 500, summary: true })).toEqual({
      prev: 'a', curr: 'b', path: 'bk', rows: [], ...totals, expansions: 0, truncated: false, threshold: 19, ...meta,
    })
  })
})
