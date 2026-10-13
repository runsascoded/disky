import { describe, expect, it } from 'vitest'
import { HttpError } from './batches'
import { CoverError, type Resolved } from './filterCover'
import { actBlock, actController, noMatches, type ActDeps, type ActState, afterResolve, CONFIRM_OVER, ROW_CONFIRM_OVER } from './matchAct'

// The action's state machine with its sends stubbed: each test records every state it passes through, and
// every request it would send.
const dir = (path: string) => ({ path, kind: 'dir' as const, b: 10, o: 2, roots: 1 })
const file = (path: string) => ({ path, kind: 'file' as const, b: 1, o: 1, roots: 1 })
const OVER = 'Too many matches to act on at once (70,390); narrow the search or open a folder below. Agents can bulk-assign via the API.'

/** A controller over a recorded state cell; `resolve` and the sends are the test's. */
function harness(o: Partial<ActDeps> & { got?: Resolved | (() => Promise<Resolved>) }) {
  const states: ActState[] = []
  let cur: ActState = { s: 'idle' }
  const sent: unknown[] = []
  const landed: string[] = []
  let b = 0
  const deps: ActDeps = {
    scheme: 'gs://',
    resolve: typeof o.got === 'function' ? o.got : async () => o.got as Resolved,
    assign: async a => { sent.push(['assign', a.length, a[0]]) },
    call: (async (url: string, method: string, body: { prefixes?: string[]; objects?: string[] }) => {
      sent.push([method, url, body.prefixes?.length ?? body, body.objects?.length ?? 0])
      if (url.endsWith('/stage')) return { plan_id: 4, batch_id: ++b, staged: [...(body.prefixes ?? []), ...(body.objects ?? [])], ...(body.objects ? { staged_objects: body.objects } : {}), covered: [], absorbed: [], as_of: 'S' }
      return {}
    }) as never,
    asOf: () => 'S',
    landed: k => landed.push(k),
    ...o,
  }
  const ctl = actController(() => deps, s => { cur = s; states.push(s) }, () => cur)
  const shape = () => states.map(s => s.s === 'sending' ? `sending ${s.batch}/${s.batches}` : s.s === 'done' ? `done: ${s.text}${s.undo ? ' (undo)' : ''}` : s.s === 'muted' ? `muted: ${s.reason}` : s.s === 'failed' ? `failed: ${s.msg}` : s.s)
  return { ctl, states, sent, landed, shape, get: () => cur }
}

describe('a row\'s action: offered at once, resolved on the click', () => {
  it('a small set goes straight through: listing → sending (one batch) → accepted', async () => {
    const h = harness({ got: { items: [dir('b/x/tomat'), file('b/y/tomat.txt')], complete: true } })
    await h.ctl.start({ kind: 'assign', owner: '@me', who: 'you' })
    expect([h.shape(), h.sent, h.landed]).toEqual([
      ['resolving', 'sending 0/1', 'sending 1/1', 'done: assigned 1 folder, 1 file → you'],
      [['assign', 2, { pattern: 'gs://b/x/tomat/', owner: '@me' }]],
      ['assign'],
    ])
  })
  it('a large set asks first; confirm → batches with progress → accepted (staging offers undo)', async () => {
    const items = Array.from({ length: 1100 }, (_, i) => dir(`b/d${i}/tomat`))
    const h = harness({ got: { items, complete: true } })
    await h.ctl.start({ kind: 'stage', memo: 'old runs' })
    expect(h.shape()).toEqual(['resolving', 'confirm'])
    await h.ctl.confirm()
    expect([h.shape().slice(2), h.sent]).toEqual([
      ['sending 0/3', 'sending 1/3', 'sending 2/3', 'sending 3/3', 'done: staged 1,100 folders (undo)'],
      [['POST', '/api/plans/stage', 500, 0], ['POST', '/api/plans/stage', 500, 0], ['POST', '/api/plans/stage', 100, 0], ['POST', '/api/plans/4/batches/merge', { into: 1, ids: [2, 3] }, 0]],
    ])
    await h.ctl.undo()
    expect([h.shape().slice(-2), h.sent.slice(4)]).toEqual([
      ['undoing', 'done: unstaged 1,100 items'],
      [['DELETE', '/api/plans/4/items', 500, 0], ['DELETE', '/api/plans/4/items', 500, 0], ['DELETE', '/api/plans/4/items', 100, 0]],
    ])
  })
  it('unassign (owner null) sends null, and says so; a staging that absorbed staged descendants offers no undo', async () => {
    const un = harness({ got: { items: [dir('b/x')], complete: true } })
    await un.ctl.start({ kind: 'assign', owner: null, who: 'nobody' })
    const absorbing = harness({
      got: { items: [dir('b/x')], complete: true },
      call: (async () => ({ plan_id: 4, batch_id: 1, staged: ['gs://b/x/'], covered: [], absorbed: ['gs://b/x/y/'], as_of: 'S' })) as never,
    })
    await absorbing.ctl.start({ kind: 'stage' })
    expect([un.sent, un.shape().at(-1), absorbing.shape().at(-1)]).toEqual([
      [['assign', 1, { pattern: 'gs://b/x/', owner: null }]],
      'done: unassigned 1 folder',
      'done: staged 1 folder',
    ])
  })
  it('cancel at the confirm sends nothing', async () => {
    const h = harness({ got: { items: Array.from({ length: CONFIRM_OVER + 1 }, (_, i) => dir(`b/d${i}`)), complete: true } })
    await h.ctl.start({ kind: 'stage' })
    h.ctl.cancel()
    expect([h.shape(), h.sent]).toEqual([['resolving', 'confirm', 'idle'], []])
  })
  it('a whole bucket\'s assignment always asks, however small', () => {
    const d = { scheme: 'gs://' }
    expect([
      afterResolve({ items: [dir('tomatoes')], complete: true }, { kind: 'assign' }, d).s,
      afterResolve({ items: [dir('b/tomatoes')], complete: true }, { kind: 'assign' }, d).s,
      afterResolve({ items: Array.from({ length: CONFIRM_OVER }, (_, i) => dir(`b/d${i}`)), complete: true }, { kind: 'stage' }, d).s,
      afterResolve({ items: Array.from({ length: CONFIRM_OVER + 1 }, (_, i) => dir(`b/d${i}`)), complete: true }, { kind: 'stage' }, d).s,
    ]).toEqual(['confirm', 'go', 'go', 'confirm'])
  })
})

describe('a table row\'s action asks before sending more than one item', () => {
  // gcs prod, `?f=tomat`: the row labelled `marin-us-east5/tomat` resolved to 9 items; "me" sent all 9 at once.
  const tomat = dir('marin-us-east5/tomat')
  const flan = Array.from({ length: 8 }, (_, i) => file(`marin-us-east5/data/hrm_text_split/flan_direct/flan_${i}_rotten_tomatoes_part_00000.parquet`))
  const row = { scheme: 'gs://', confirmOver: ROW_CONFIRM_OVER }
  const go = (items: ReturnType<typeof dir | typeof file>[], d: { scheme: string; confirmOver?: number } = row) => afterResolve({ items, complete: true }, { kind: 'assign' }, d).s
  it('the 9-item row asks first, and sends nothing until confirmed', async () => {
    const h = harness({ got: { items: [tomat, ...flan], complete: true }, confirmOver: ROW_CONFIRM_OVER })
    await h.ctl.start({ kind: 'assign', owner: '@me', who: 'you' })
    expect([h.shape(), h.sent]).toEqual([['resolving', 'confirm'], []])
    await h.ctl.confirm()
    expect([h.shape().at(-1), h.sent]).toEqual(['done: assigned 1 folder, 8 files → you', [['assign', 9, { pattern: 'gs://marin-us-east5/tomat/', owner: '@me' }]]])
  })
  it('one item goes straight through; two or more ask; the default threshold is unchanged elsewhere', () => {
    expect([
      go([tomat]),
      go([flan[0]]),
      go([tomat, flan[0]]),
      go([tomat, ...flan]),
      go([tomat, ...flan], { scheme: 'gs://' }),
    ]).toEqual(['go', 'go', 'confirm', 'confirm', 'go'])
  })
})

describe('not acted on: muted in place (never red), unless it failed', () => {
  it('over the cap after resolving: the reason, muted', async () => {
    const h = harness({ got: { items: [], complete: false, reason: OVER } })
    await h.ctl.start({ kind: 'stage' })
    expect([h.shape(), h.sent]).toEqual([['resolving', `muted: ${OVER}`], []])
  })
  it('nothing sendable (only unchecked matches; only whole buckets to stage): muted, saying why', async () => {
    const unk = harness({ got: { items: [{ path: 'b/x', kind: null, b: 1, o: 1, roots: 1 }], complete: true } })
    await unk.ctl.start({ kind: 'assign' })
    const bk = harness({ got: { items: [dir('tomatoes')], complete: true } })
    await bk.ctl.start({ kind: 'stage' })
    expect([unk.shape(), bk.shape()]).toEqual([
      ['resolving', 'muted: These matches couldn’t be checked (file or folder?); open the folder to act on them.'],
      ['resolving', 'muted: Whole buckets can’t be staged for deletion; open the bucket to act on its matches.'],
    ])
  })
  it('a refused cover (4xx) is muted; a 5xx or the network is red, and retry starts over', async () => {
    const refused = harness({ got: () => Promise.reject(new CoverError('A filter with exclusions can’t be turned into whole folders or files to act on; drop the exclusion to act on the matches.', 400)) })
    await refused.ctl.start({ kind: 'stage' })
    let n = 0
    const flaky = harness({ got: () => (n++ ? Promise.resolve({ items: [dir('b/x')], complete: true }) : Promise.reject(new CoverError('filter-cover: 503', 503))) })
    await flaky.ctl.start({ kind: 'stage' })
    const net = harness({ got: () => Promise.reject(new TypeError('Failed to fetch')) })
    await net.ctl.start({ kind: 'assign' })
    expect(refused.shape()).toEqual(['resolving', 'muted: A filter with exclusions can’t be turned into whole folders or files to act on; drop the exclusion to act on the matches.'])
    expect(net.shape()).toEqual(['resolving', 'failed: Failed to fetch'])
    expect(flaky.shape()).toEqual(['resolving', 'failed: filter-cover: 503'])
    await flaky.ctl.retry()
    expect(flaky.shape().slice(2)).toEqual(['idle', 'resolving', 'sending 0/1', 'sending 1/1', 'done: staged 1 folder (undo)'])
  })
  it('a send refused (403) is muted; a send that fails (500) is red', async () => {
    const forbid = harness({ got: { items: [dir('b/x')], complete: true }, assign: () => Promise.reject(new HttpError('not allowed', 403)) })
    await forbid.ctl.start({ kind: 'assign' })
    const down = harness({ got: { items: [dir('b/x')], complete: true }, assign: () => Promise.reject(new HttpError('D1 down', 503)) })
    await down.ctl.start({ kind: 'assign' })
    expect([forbid.shape(), down.shape(), forbid.landed, down.landed]).toEqual([
      ['resolving', 'sending 0/1', 'muted: not allowed'],
      ['resolving', 'sending 0/1', 'failed: D1 down'],
      [], [],
    ])
  })
  it('a second click while one is in flight is ignored; a cancel mid-listing drops the late answer', async () => {
    let release: (r: Resolved) => void = () => {}
    const h = harness({ got: () => new Promise<Resolved>(r => { release = r }) })
    const p = h.ctl.start({ kind: 'stage' })
    void h.ctl.start({ kind: 'assign' })
    h.ctl.cancel()
    release({ items: [dir('b/x')], complete: true })
    await p
    expect([h.shape(), h.sent]).toEqual([['resolving', 'idle'], []])
  })
})

describe('actBlock: which filtered views offer no action on their matches', () => {
  it('a refusal, a catalog (per-bucket) answer, a rollup, a dir-only scan, an approximate read, no matches — each its reason, in that precedence; a listed view: none', () => {
    expect([
      actBlock({ refused: true, rollup: true, bucketsOnly: true, approximate: true }),
      actBlock({ rollup: true, bucketsOnly: true }),
      actBlock({ bucketsOnly: 'scoped' }),
      actBlock({ rollup: true }),
      actBlock({ dirsOnly: true, approximate: true }),
      actBlock({ approximate: true }),
      actBlock({ approximate: true, none: true }),
      actBlock({ dirsOnly: true, none: true }),
      actBlock({ none: true }),
      actBlock({}),
      actBlock({ refused: false, rollup: false, bucketsOnly: false, approximate: false, none: false }),
    ]).toEqual([
      'This view was refused, so it has no matches to act on. Narrow the term, or drill to where it is answered.',
      'Only per-bucket totals are known for this term here, not its matches. Narrow the term to act on them.',
      'Only per-bucket totals are known for this term here, not its matches. Narrow the term to act on them.',
      'This term is too common to list here: the view shows per-folder totals, not its matches. Narrow the term, or drill in.',
      'This scan lists folders only, so its file matches aren’t known here. Pick a newer scan to act on the matches.',
      'These matches are approximate (read without the search index), so some may be missing. Narrow the term, or drill in.',
      'These matches are approximate (read without the search index), so some may be missing. Narrow the term, or drill in.',
      'This scan lists folders only, so its file matches aren’t known here. Pick a newer scan to act on the matches.',
      'Nothing matches this term here, so there is nothing to act on. Widen the term, or drill out.',
      null,
      null,
    ])
  })
})

describe('noMatches: a filtered root with nothing under it', () => {
  it('empty (no bytes, objects or children): true; not loaded, or any bytes, objects (zero-byte files) or children: false', () => {
    expect([
      noMatches({ b: 0, o: 0 }),
      noMatches({ b: 0, o: 0, c: [] }),
      noMatches(null),
      noMatches(undefined),
      noMatches({ b: 5, o: 1 }),
      noMatches({ b: 0, o: 2 }),
      noMatches({ b: 0, o: 0, c: [{}] }),
    ]).toEqual([true, true, false, false, false, false, false])
  })
})
