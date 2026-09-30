import { describe, expect, it } from 'vitest'
import { createPerf, parseServerTiming, type PerfDeps, type PerfEntry } from './perf'

// A hand-cranked clock + User Timing sink: marks/measures are recorded, rAF
// callbacks and timers run only when the test says so.
function fake(settleMs = 500) {
  let now = 1000
  const marks: { name: string; startTime: number; detail?: unknown }[] = []
  const measures: { name: string; start: number; end: number }[] = []
  let rafs: (() => void)[] = []
  let timers: { id: number; fn: () => void; at: number }[] = []
  let nextTimer = 1
  const deps: PerfDeps = {
    now: () => now,
    mark: (name, o) => { marks.push('detail' in o ? { name, startTime: o.startTime, detail: o.detail } : { name, startTime: o.startTime }) },
    measure: (name, o) => { measures.push({ name, start: o.start, end: o.end }) },
    raf: fn => { rafs.push(fn) },
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.push({ id, fn, at: now + ms }); return id },
    clearTimeout: t => { timers = timers.filter(x => x.id !== t) },
  }
  const perf = createPerf(deps, settleMs)
  return {
    perf, marks, measures,
    /** Advance the clock; run every rAF callback queued so far. */
    frame(dt = 16) { now += dt; const fs = rafs; rafs = []; for (const f of fs) f() },
    /** Advance the clock, firing timers as their time comes. */
    tick(dt: number) {
      const until = now + dt
      for (;;) {
        const due = timers.filter(t => t.at <= until).sort((a, b) => a.at - b.at)[0]
        if (!due) break
        timers = timers.filter(t => t.id !== due.id)
        now = due.at
        due.fn()
      }
      now = until
    },
    advance(dt: number) { now += dt },
    pendingRafs: () => rafs.length,
  }
}

const res = (headers: Record<string, string>, status = 200) => ({
  status,
  headers: { get: (n: string) => headers[n.toLowerCase()] ?? null },
})

describe('parseServerTiming', () => {
  it('reads the edge format: dur phases (counts too) and desc entries', () => {
    expect(parseServerTiming('fetch;dur=812, spans;dur=40, ngroups;dur=3, cache;desc=hit, total;dur=901')).toEqual({
      phases: { fetch: 812, spans: 40, ngroups: 3, total: 901 },
      desc: { cache: 'hit' },
    })
  })
  it('tolerates quoted values, mixed-case params, entries without params and an absent header', () => {
    expect(parseServerTiming('db;DUR="12.5";desc="d1 query", edge,  , x;dur=nope')).toEqual({
      phases: { db: 12.5 },
      desc: { db: 'd1 query' },
    })
    expect(parseServerTiming(null)).toEqual({ phases: {}, desc: {} })
    expect(parseServerTiming('')).toEqual({ phases: {}, desc: {} })
  })
})

describe('createPerf', () => {
  it('stamps the five marks and four measures of one load, and reports the spans', async () => {
    const f = fake()
    const h = f.perf.start('series', 'ctbk|n3')
    f.advance(120)
    const r = res({ 'server-timing': 'fetch;dur=80, total;dur=95', 'x-cache': 'miss' })
    await h.track(Promise.resolve(r))
    f.advance(30)
    h.decoded()
    f.advance(5)
    f.perf.commit('series')
    f.frame(10)
    f.tick(500)
    expect(f.marks).toEqual([
      { name: 'series:ctbk|n3:request', startTime: 1000 },
      { name: 'series:ctbk|n3:response', startTime: 1120, detail: { server: { fetch: 80, total: 95 }, cache: 'miss', status: 200 } },
      { name: 'series:ctbk|n3:decoded', startTime: 1150 },
      { name: 'series:ctbk|n3:painted', startTime: 1165 },
      { name: 'series:ctbk|n3:settled', startTime: 1165 },
    ])
    expect(f.measures).toEqual([
      { name: 'series:ctbk|n3:wait', start: 1000, end: 1120 },
      { name: 'series:ctbk|n3:decode', start: 1120, end: 1150 },
      { name: 'series:ctbk|n3:paint', start: 1150, end: 1165 },
      { name: 'series:ctbk|n3:settle', start: 1165, end: 1165 },
      { name: 'series:ctbk|n3:total', start: 1000, end: 1165 },
    ])
    const expected: PerfEntry[] = [{
      id: 1, widget: 'series', key: 'ctbk|n3', start: 1000,
      wait: 120, decode: 30, paint: 15, settle: 0, total: 165,
      server: { fetch: 80, total: 95 }, cache: 'miss', status: 200, done: true, failed: false, empty: false,
    }]
    expect(f.perf.entries()).toEqual(expected)
  })

  it('settles at the LAST commit\'s frame, not at the end of the quiet window', () => {
    const f = fake()
    const h = f.perf.start('treemap', '/@d')
    f.advance(100)
    h.response(res({}))
    h.decoded()
    f.perf.commit('treemap')
    f.frame(10)                    // painted @1110
    f.tick(300)                    // a later graft lands…
    f.perf.commit('treemap')
    f.frame(10)                    // …and paints @1420
    f.tick(489)                    // still inside the re-armed window: nothing settles
    expect(f.perf.entries()[0].done).toBe(false)
    f.tick(1)                      // window closes: settled = the 1420 frame
    expect(f.perf.entries()[0]).toMatchObject({ wait: 100, decode: 0, paint: 10, settle: 310, total: 420, done: true })
    expect(f.marks.map(m => [m.name.split(':')[2], m.startTime])).toEqual([
      ['request', 1000], ['response', 1100], ['decoded', 1100], ['painted', 1110], ['settled', 1420],
    ])
  })

  it('a commit before decode neither paints nor counts; only decoded loads of that widget paint', () => {
    const f = fake()
    const a = f.perf.start('treemap', 'a')
    const b = f.perf.start('treemap', 'b')
    f.perf.commit('treemap')       // nothing decoded yet
    f.frame(10)
    f.tick(500)
    expect(f.perf.entries().map(e => [e.key, e.paint, e.done])).toEqual([['a', null, false], ['b', null, false]])
    a.response(res({}))
    a.decoded()
    f.perf.commit('treemap')
    f.frame(10)
    f.tick(500)
    expect(f.perf.entries().map(e => [e.key, e.paint, e.done])).toEqual([['a', 10, true], ['b', null, false]])
    b.response(res({}))
    b.decoded()
    f.advance(50)
    f.perf.commit('treemap')
    f.frame(10)
    f.tick(500)
    expect(f.perf.entries().map(e => [e.key, e.paint, e.done])).toEqual([['a', 10, true], ['b', 60, true]])
  })

  it('twins share request/response/decoded and paint on their own widget\'s commits', () => {
    const f = fake()
    const h = f.perf.start('treemap', '/ctbk@d', ['table'])
    f.advance(200)
    h.response(res({ 'x-cache': 'hit' }))
    f.advance(20)
    h.decoded()
    f.perf.commit('treemap')
    f.frame(10)                    // map painted @1230
    f.advance(40)
    f.perf.commit('table')
    f.frame(10)                    // table painted @1280
    f.tick(500)
    expect(f.perf.entries()).toEqual([
      { id: 1, widget: 'treemap', key: '/ctbk@d', start: 1000, wait: 200, decode: 20, paint: 10, settle: 0, total: 230, server: {}, cache: 'hit', status: 200, done: true, failed: false, empty: false },
      { id: 2, widget: 'table', key: '/ctbk@d', start: 1000, wait: 200, decode: 20, paint: 60, settle: 0, total: 280, server: {}, cache: 'hit', status: 200, done: true, failed: false, empty: false },
    ])
    expect(f.marks.map(m => m.name)).toEqual([
      'treemap:/ctbk@d:request', 'table:/ctbk@d:request',
      'treemap:/ctbk@d:response', 'table:/ctbk@d:response',
      'treemap:/ctbk@d:decoded', 'table:/ctbk@d:decoded',
      'treemap:/ctbk@d:painted',
      'table:/ctbk@d:painted',
      'treemap:/ctbk@d:settled',
      'table:/ctbk@d:settled',
    ])
  })

  it('a rejected fetch marks the load failed with no response', async () => {
    const f = fake()
    const h = f.perf.start('age', '/@d|b640')
    await expect(h.track(Promise.reject(new Error('aborted')))).rejects.toThrow('aborted')
    expect(f.perf.entries()).toEqual([{
      id: 1, widget: 'age', key: '/@d|b640', start: 1000,
      wait: null, decode: null, paint: null, settle: null, total: null,
      server: {}, cache: null, status: null, done: false, failed: true, empty: false,
    }])
    expect(f.marks.map(m => m.name)).toEqual(['age:/@d|b640:request'])
  })

  it('falls back to the commit time when no frame ever comes (hidden tab)', () => {
    const f = fake()
    const h = f.perf.start('dtm', 'p', ['dtable'])
    f.advance(100)
    h.response(res({ 'server-timing': 'cache;desc=hit' }))
    h.decoded()
    f.advance(10)
    f.perf.commit('dtm')           // @1110, rAF never runs
    f.tick(500)
    expect(f.pendingRafs()).toBe(1)
    expect(f.perf.entries().map(e => [e.widget, e.paint, e.settle, e.total, e.done, e.cache])).toEqual([
      ['dtm', 10, 0, 110, true, 'hit'],
      ['dtable', null, null, null, false, 'hit'],
    ])
  })

  it('empty() closes a load at decode: painted and settled stamped there, no commit needed', () => {
    const f = fake()
    const h = f.perf.start('dtm', 'p@a→b', ['dtable'])
    f.advance(300)
    h.response(res({}))
    f.advance(2)
    h.empty()
    f.tick(1000)
    expect(f.perf.entries()).toEqual([
      { id: 1, widget: 'dtm', key: 'p@a→b', start: 1000, wait: 300, decode: 2, paint: 0, settle: 0, total: 302, server: {}, cache: null, status: 200, done: true, failed: false, empty: true },
      { id: 2, widget: 'dtable', key: 'p@a→b', start: 1000, wait: 300, decode: 2, paint: 0, settle: 0, total: 302, server: {}, cache: null, status: 200, done: true, failed: false, empty: true },
    ])
    expect(f.marks.filter(m => m.name.startsWith('dtm:')).map(m => [m.name.split(':')[2], m.startTime])).toEqual([
      ['request', 1000], ['response', 1300], ['decoded', 1302], ['painted', 1302], ['settled', 1302],
    ])
  })

  it('subscribers hear every stamp; reset empties the ledger', () => {
    const f = fake()
    let n = 0
    const off = f.perf.subscribe(() => { n++ })
    const h = f.perf.start('series', 'k')
    h.response(res({}))
    h.decoded()
    f.perf.commit('series')
    f.frame()
    f.tick(500)
    expect(n).toBe(5)
    f.perf.reset()
    expect(n).toBe(6)
    expect(f.perf.entries()).toEqual([])
    off()
    f.perf.start('series', 'k2')
    expect(n).toBe(6)
  })
})
