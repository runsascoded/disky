import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunOutcome } from '../../src/runs.js'
import { closedParent, countedItems, PLAN_SENDER, QUEUE_EMPTIED, queueWasEmpty, queuedOf, fmtBytes, personSender, renderParent, runEvent, runUrl, sendParent, shortPath, sizeOf, stageReply, stagedCardUrl, topFolders, type RunRow } from './stagedSlack.js'
import { sqliteD1 } from './testD1.js'

const run = (o: Partial<RunRow>): RunRow => ({
  run_id: 'cw-sweep-dry-1', mode: 'dry', scan: '2026-09-28T1201', actor: 'ann@staff.example.test', started_ts: 100,
  finished_ts: 200, deleted_bytes: 2 * 1024 ** 4, deleted_objects: 1234, skipped_gone: 0, skipped_overwritten: 0,
  plan_digest: 'D1', undo_deadline: null, ...o,
})

const ids = (blocks: unknown[]): string[] =>
  ((blocks.find(b => (b as { type: string }).type === 'actions') as { elements: { action_id: string }[] }).elements).map(e => e.action_id)

describe('renderParent', () => {
  const base = { planId: 7, siteUrl: 'https://cw-s3.oa.dev', items: 3, batches: 2, stagers: ['ann@staff.example.test', 'bo@viewer.example.test'], digest: 'D1', actions: true, closed: false }

  describe('Delete for real is a link to /staged, never a dispatch', () => {
    const OPEN = { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'Open in www' }, url: 'https://cw-s3.oa.dev/staged' }
    const DRY = { type: 'button', action_id: 'staged_dry', value: '7', text: { type: 'plain_text', text: 'Dry-run' } }
    const LINK = { type: 'button', action_id: 'staged_delete_link', text: { type: 'plain_text', text: 'Delete for real…' }, url: 'https://cw-s3.oa.dev/staged' }
    const buttons = (runs: RunRow[]) => (renderParent({ ...base, runs }).blocks.find(b => (b as { type: string }).type === 'actions') as { elements: unknown[] }).elements
    it.each<[string, RunRow[]]>([
      ['with a matching dry run', [run({})]],
      ['with no dry run: a real run needs none', []],
      ['with a dry run of a set since changed', [run({ plan_digest: 'OLD' })]],
    ])('%s', (_, runs) => expect(buttons(runs)).toEqual([OPEN, DRY, LINK]))
  })

  it('shows only the www link when dispatch is not wired, the plan is closed, or empty', () => {
    expect(ids(renderParent({ ...base, runs: [run({})], actions: false }).blocks)).toEqual(['staged_open'])
    expect(ids(renderParent({ ...base, runs: [run({})], closed: true }).blocks)).toEqual(['staged_open'])
    expect(ids(renderParent({ ...base, runs: [], items: 0 }).blocks)).toEqual(['staged_open'])
  })

  it('says only what bears on the current set: no stale dry-run, no real run past its undo window', () => {
    const txt = (runs: RunRow[], now = 1000): string | null => (renderParent({ ...base, runs, now }).blocks.filter(b => (b as { type: string }).type === 'section')[1] as { text: { text: string } } | undefined)?.text.text ?? null
    const real = run({ mode: 'real', run_id: 'r', plan_digest: 'OLD', undo_deadline: 2000, started_ts: 50 })
    expect([
      txt([]),
      txt([run({})]),
      txt([run({ plan_digest: 'OLD' })]),
      txt([run({ plan_digest: '', deleted_bytes: 0, deleted_objects: 0 })]),
      txt([run({ finished_ts: null })]),
      txt([run({}), real]),
      txt([run({}), real], 3000),
      txt([run({}), { ...real, undo_deadline: null }]),
      txt([run({}), { ...real, finished_ts: null }]),
    ]).toEqual([
      null,
      'Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — matches the current plan.',
      null,
      'Latest dry-run (`cw-sweep-dry-1`) ended without a result.',
      'Dry-run running (ann, scan 2026-09-28T1201).',
      'Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — matches the current plan.\nLast real run deleted 2.0 TiB / 1,234 objects, undoable until 1970-01-01 00:33Z.',
      'Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — matches the current plan.',
      'Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — matches the current plan.',
      'Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — matches the current plan.\nReal run in progress (ann).',
    ])
  })

  it('leaves out the status section when there is nothing to say (Slack refuses an empty one)', () => {
    expect([false, true].map(actions => renderParent({ ...base, runs: [], actions }).blocks.map(b => (b as { type: string }).type))).toEqual([['section', 'actions'], ['section', 'actions']])
  })

  it('titles by what is still queued (deleted items drop out)', () => {
    const head = (v: Partial<Parameters<typeof renderParent>[0]>): string => (renderParent({ ...base, runs: [], ...v }).blocks[0] as { text: { text: string } }).text.text
    expect([
      head({}),
      head({ queued: { items: 1, batches: 1, stagers: ['bo@viewer.example.test'] } }),
    ]).toEqual([
      '*Staged for deletion* · plan #7: *3 prefixes*\nstaged by ann, bo',
      '*Staged for deletion* · plan #7: *1 prefix*\nstaged by bo',
    ])
  })
})

describe('sendParent', () => {
  const IMG = 'https://site.example.org/og/staged.png?v=abc&sig=x'
  const parent = renderParent({ planId: 7, siteUrl: 'https://site.example.org', items: 3, batches: 1, stagers: [], digest: 'D1', actions: true, closed: false, runs: [], image: IMG })
  /** Slack refusing any message with an image block until the card has been fetched; every call in order. */
  function slack({ cardOk = true } = {}): string[] {
    const calls: string[] = []
    let rendered = false
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      if (input === IMG) {
        calls.push('fetch card')
        rendered = cardOk
        return new Response('png', { status: cardOk ? 200 : 503 })
      }
      const body = JSON.parse(String(init!.body)) as { ts?: string; blocks: { type: string }[] }
      const image = body.blocks.some(b => b.type === 'image')
      const refused = image && !rendered
      calls.push(`${input.split('/').pop()} ${body.ts ?? 'new'} ${image ? 'card' : 'bare'} ${refused ? 'refused' : 'ok'}`)
      return new Response(JSON.stringify(refused ? { ok: false, error: 'invalid_blocks', response_metadata: { messages: ['[ERROR] downloading image failed'] } } : { ok: true, ts: '200.000001' }))
    }))
    return calls
  }
  const env = { SLACK_BOT_TOKEN: 'xoxb' }
  afterEach(() => { vi.unstubAllGlobals() })

  it('a refused card: the text lands without it, then the card is rendered and put back', async () => {
    const calls = slack()
    const got = await sendParent(env, 'C', '1.000001', parent)
    expect([got.ok, calls]).toEqual([true, ['chat.update 1.000001 card refused', 'chat.update 1.000001 bare ok', 'fetch card', 'chat.update 1.000001 card ok']])
  })
  it('a card that won\'t render: the text stays, no second try', async () => {
    const calls = slack({ cardOk: false })
    await sendParent(env, 'C', '1.000001', parent)
    expect(calls).toEqual(['chat.update 1.000001 card refused', 'chat.update 1.000001 bare ok', 'fetch card'])
  })
  it('a first post, refused: posted bare, then updated with the card at the new ts', async () => {
    const calls = slack()
    const got = await sendParent(env, 'C', null, parent)
    expect([got.ts, calls]).toEqual(['200.000001', ['chat.postMessage new card refused', 'chat.postMessage new bare ok', 'fetch card', 'chat.update 200.000001 card ok']])
  })
  it('any other failure is returned as it is: dropping the card wouldn\'t help', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      calls.push(input)
      return new Response(JSON.stringify({ ok: false, error: 'message_not_found' }))
    }))
    expect([(await sendParent(env, 'C', '1.000001', parent)).error, calls]).toEqual(['message_not_found', ['https://slack.com/api/chat.update']])
  })
})

describe('run events', () => {
  it('reads as a sentence per phase', () => {
    const site = 'https://cw-s3.oa.dev'
    const link = '<https://cw-s3.oa.dev/staged?run=cw-sweep-dry-1|cw-sweep-dry-1>'
    expect(runEvent(run({ finished_ts: null }), 'dispatched', { via: 'Slack' })).toBe(':test_tube: Dry-run dispatched by ann via Slack on scan 2026-09-28T1201 (`cw-sweep-dry-1`)')
    expect(runEvent(run({ finished_ts: null }), 'dispatched', { via: 'www', names: { 'ann@staff.example.test': 'Ann Example' }, siteUrl: site }))
      .toBe(`:test_tube: Dry-run dispatched by Ann Example via www on scan 2026-09-28T1201 (${link})`)
    expect(runEvent(run({ skipped_gone: 2 }), 'finished', { siteUrl: site })).toBe(`:test_tube: Dry-run ${link} finished: would delete *2.0 TiB* / 1,234 objects (gone since scan: 2, overwritten: 0).`)
    expect(runEvent(run({ mode: 'real', run_id: 'cw-sweep-real-1', undo_deadline: 1_790_604_800 }), 'finished')).toBe(':white_check_mark: Real deletion `cw-sweep-real-1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
    expect(runEvent(run({ plan_digest: '' }), 'failed', { reason: 'b1: `x` <y> & z' })).toBe(":x: Dry-run `cw-sweep-dry-1` ended without a result: `b1: 'x' &lt;y&gt; &amp; z`")
    expect(runEvent(run({ plan_digest: '' }), 'failed', { siteUrl: site })).toBe(`:x: Dry-run ${link} ended without a result (its Batch job stopped before the run summary); check its logs in www.`)
    expect(runUrl(site, '2026-09-28-p1/20260928T120000Z')).toBe('https://cw-s3.oa.dev/staged?run=2026-09-28-p1%2F20260928T120000Z')
    expect([fmtBytes(0), fmtBytes(1536), fmtBytes(3 * 1024 ** 3)]).toEqual(['0 B', '1.5 KiB', '3.0 GiB'])
  })

  describe('the plan card\'s last real run, by what its run dir says', () => {
    const base = { planId: 7, siteUrl: 'https://s', items: 1, batches: 1, stagers: [], digest: 'D1', actions: false, closed: false }
    const real = run({ mode: 'real', run_id: 'r1', undo_deadline: 1_790_604_800 })
    const line = (lastOutcome?: RunOutcome | null, r: RunRow = real) => (renderParent({ ...base, runs: [r], lastOutcome, now: 1_790_000_000 }).blocks as { text?: { text?: string } }[])
      .flatMap(b => (b.text?.text ?? '').split('\n')).find(l => /^(Last real run|Real run)/.test(l))
    const SKIPPED = { bytes: 3 * 1024 ** 3, objects: 5678 }
    it('a listing run, or one with no file: deleted', () => {
      expect(line()).toBe('Last real run deleted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.')
      expect(line(null)).toBe('Last real run deleted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.')
    })
    it('a generation run: attempted', () => {
      expect(line({ attempted: true, skipped: null, notSwept: [] })).toBe('Last real run attempted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.')
    })
    it('its skips', () => {
      expect(line({ attempted: true, skipped: SKIPPED, notSwept: ['b1'] })).toBe('Last real run attempted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z. Skipped, no generation: *3.0 GiB* / 5,678 objects left in place. *Not swept:* `b1` (no object in them had a generation; their prefixes stay staged).')
    })
    it('unread: only what D1 recorded', () => {
      expect(line({ attempted: false, skipped: null, notSwept: [], unread: true })).toBe('Last real run recorded 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.')
    })
    it('closed with no final summary: at least', () => {
      expect(line({ attempted: true, skipped: null, notSwept: [], totals: 'lower-bound' })).toBe('Last real run attempted at least 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.')
    })
    it('still running: no outcome to say', () => {
      expect(line({ attempted: true, skipped: null, notSwept: [] }, { ...real, finished_ts: null })).toBe('Real run in progress (ann).')
    })
  })

  describe('a real run with no undo never promises one', () => {
    const noUndo = run({ mode: 'real', run_id: 'r1', undo_deadline: null, soft_delete_days: 0 })
    it('its finish says so', () => {
      expect(runEvent(noUndo, 'finished')).toBe(':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; no undo (soft delete off).')
      expect(runEvent(noUndo, 'finished', { outcome: { attempted: false, skipped: null, notSwept: [], unread: true } })).toBe(':warning: Real deletion `r1` finished, totals unread: recorded *2.0 TiB* / 1,234 objects; no undo (soft delete off).')
    })
    it('a killed one says so, and no word of what undo restores', () => {
      expect(runEvent(noUndo, 'failed', { outcome: { attempted: true, skipped: null, notSwept: [], totals: 'exact' } })).toBe(':warning: *Real deletion* `r1` ended unsuccessfully: attempted *2.0 TiB* / 1,234 objects; no undo (soft delete off).')
      expect(runEvent(noUndo, 'failed', { outcome: { attempted: true, skipped: null, notSwept: [], totals: 'lower-bound' } })).toBe(':warning: *Real deletion* `r1` ended before recording its end: attempted at least *2.0 TiB* / 1,234 objects, from its progress files, which can lag what it did; no undo (soft delete off).')
    })
    it('a window or nothing recorded: as before', () => {
      expect(runEvent({ ...noUndo, soft_delete_days: 7, undo_deadline: 1_790_604_800 }, 'finished')).toBe(':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
      expect(runEvent({ ...noUndo, soft_delete_days: null }, 'finished')).toBe(':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects.')
    })
    it('a run that required nothing and measured nothing promises no undo, and doesn\'t claim soft delete was off', () => {
      // A killed `sweep execute --for-real -s 0`: no end record, so what its buckets retain was never written.
      const unmeasured = run({ mode: 'real', run_id: 'r1', undo_deadline: null, soft_delete_days: null, min_soft_delete_days: 0 })
      expect(runEvent(unmeasured, 'failed', { outcome: { attempted: true, skipped: null, notSwept: [], totals: 'lower-bound' } })).toBe(':warning: *Real deletion* `r1` ended before recording its end: attempted at least *2.0 TiB* / 1,234 objects, from its progress files, which can lag what it did; no undo promised (no soft-delete minimum).')
    })
    it('the parent has no "undoable" line for it', () => {
      const status = (renderParent({ planId: 7, siteUrl: 'https://x', items: 3, batches: 1, stagers: [], digest: 'D1', actions: false, closed: false, runs: [noUndo], now: 1000 }).blocks as { text?: { text: string } }[]).map(b => b.text?.text ?? '')
      expect(status.filter(t => /undoable/.test(t))).toEqual([])
    })
  })

  describe('a finished run, by what its `plan-summary.json` says', () => {
    const real = run({ mode: 'real', run_id: 'r1', undo_deadline: 1_790_604_800 })
    const finished = (outcome?: RunOutcome, r: RunRow = real) => runEvent(r, 'finished', { outcome })
    const SKIPPED = { bytes: 3 * 1024 ** 3, objects: 5678 }
    it('a generation run attempted; no outcome (an older run, cw\'s, a failed read) keeps "deleted"', () => {
      expect(finished({ attempted: true, skipped: null, notSwept: [] })).toBe(':white_check_mark: Real deletion `r1` finished: attempted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
      expect(finished(undefined)).toBe(':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
      expect(finished({ attempted: false, skipped: null, notSwept: [], unread: true })).toBe(':warning: Real deletion `r1` finished, totals unread: recorded *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
    })
    it('skips are said, and it no longer reads as a clean success', () => {
      expect(finished({ attempted: true, skipped: SKIPPED, notSwept: [] })).toBe(':warning: Real deletion `r1` finished: attempted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www). Skipped, no generation: *3.0 GiB* / 5,678 objects left in place.')
    })
    it('names the buckets not swept', () => {
      expect(finished({ attempted: true, skipped: SKIPPED, notSwept: ['b1', 'b2'] })).toBe(':warning: Real deletion `r1` finished: attempted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www). Skipped, no generation: *3.0 GiB* / 5,678 objects left in place. *Not swept:* `b1`, `b2` (no object in them had a generation; their prefixes stay staged).')
    })
    it('a run with no undo deadline ends its totals with the same stop', () => {
      expect(finished({ attempted: true, skipped: SKIPPED, notSwept: [] }, { ...real, undo_deadline: null })).toBe(':warning: Real deletion `r1` finished: attempted *2.0 TiB* / 1,234 objects. Skipped, no generation: *3.0 GiB* / 5,678 objects left in place.')
    })
    it('a dry run keeps its wording, with its skips', () => {
      expect(finished({ attempted: true, skipped: SKIPPED, notSwept: [] }, run({}))).toBe(':test_tube: Dry-run `cw-sweep-dry-1` finished: would delete *2.0 TiB* / 1,234 objects (gone since scan: 0, overwritten: 0). Skipped, no generation: *3.0 GiB* / 5,678 objects left in place.')
    })
    describe('a real run closed from what it wrote', () => {
      const killed = { ...real, plan_digest: '' }
      const closed = (totals: RunOutcome['totals'], attempted = true, reason?: string) =>
        runEvent(killed, 'failed', { reason, outcome: { attempted, skipped: null, notSwept: [], totals } })
      it('its progress files: at least, and undo restores only what its log names', () => {
        expect(closed('lower-bound')).toBe(':warning: *Real deletion* `r1` ended before recording its end: attempted at least *2.0 TiB* / 1,234 objects, from its progress files, which can lag what it did; undoable until 2026-09-28 14:13Z (www). Undo restores only what its log names.')
        expect(closed('lower-bound', false, 'exit `137`')).toBe(":warning: *Real deletion* `r1` ended before recording its end (`exit '137'`): deleted at least *2.0 TiB* / 1,234 objects, from its progress files, which can lag what it did; undoable until 2026-09-28 14:13Z (www). Undo restores only what its log names.")
      })
      it('its summary: the totals as they are', () => {
        expect(closed('exact')).toBe(':warning: *Real deletion* `r1` ended unsuccessfully: attempted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
      })
      it('nothing readable: says so, and doesn\'t say none', () => {
        expect(closed('unread')).toBe(":warning: *Real deletion* `r1` ended before recording its end, and what it wrote couldn't be read, so it may have deleted more than its recorded *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www). Undo restores only what its log names.")
      })
    })
    it('a failed run says nothing of an outcome', () => {
      expect(runEvent({ ...real, plan_digest: '' }, 'failed', { outcome: { attempted: true, skipped: SKIPPED, notSwept: ['b1'] } })).toBe(':x: *Real deletion* `r1` ended without a result (its Batch job stopped before the run summary); check its logs in www.')
    })
  })
})

describe('names and sizes (plain text, never a mention)', () => {
  const base = { planId: 7, siteUrl: 'https://cw-s3.oa.dev', items: 3, batches: 2, stagers: [] as string[], digest: 'D1', actions: true, closed: false, runs: [] as RunRow[] }
  const size = { scan: '2026-10-02', b: 51 * 2 ** 40, o: 179327698, empty: 5, owners: [{ label: 'Grace Hopper', b: 16 * 2 ** 40 }, { label: 'hedy-lamarr', b: 6 * 2 ** 40 }] }
  it('the first line states the total; owners only when they hold most of it; stagers by Slack name (else the local part)', () => {
    const v = { ...base, stagers: ['A.B@example.org', 'c.d@example.org'], names: { 'a.b@example.org': 'Ann Bee' } }
    const lines = (z: typeof size) => (renderParent({ ...v, size: z }).blocks[0] as { text: { text: string } }).text.text.split('\n')
    expect([lines(size), lines({ ...size, empty: 0, o: 999, b: 40 * 2 ** 40 })]).toEqual([
      [
        '*Staged for deletion* · plan #7: *3 prefixes · 51.0 TiB · 179.3M objects* at scan 2026-10-02',
        '+5 staged prefixes empty at scan, not counted',
        'staged by Ann Bee, c.d',
      ],
      [
        '*Staged for deletion* · plan #7: *3 prefixes · 40.0 TiB · 999 objects* at scan 2026-10-02',
        'owners: Grace Hopper 16.0 TiB, hedy-lamarr 6.0 TiB',
        'staged by Ann Bee, c.d',
      ],
    ])
  })
  it('sizeOf: totals over every prefix, absent or no bytes and no objects = empty (zero-byte objects are not), owners by attributed bytes', () => {
    const at = { scan: 'S', stats: { 'gs://b/a/': { bytes: 5, objects: 2, owners: [['u1', 3], ['u2', 2]] as [string, number][] }, 'gs://b/c/': { bytes: 4, objects: 1, owners: [['u2', 4]] as [string, number][] }, 'gs://b/z/': { bytes: 0, objects: 0 }, 'gs://b/zb/': { bytes: 0, objects: 3 } } }
    expect(sizeOf(at, ['gs://b/a/', 'gs://b/c/', 'gs://b/z/', 'gs://b/zb/', 'gs://b/gone/'], u => u.toUpperCase(), 1)).toEqual({ scan: 'S', b: 9, o: 6, empty: 2, owners: [{ label: 'U2', b: 6 }] })
  })
  it('queuedOf: the title counts prefixes, batches and stagers over what is neither deleted nor empty at the scan (`countedItems`)', () => {
    const live = [
      { prefix: 'gs://b/a/', added_by: 'ann', batch_id: 1 },
      { prefix: 'gs://b/e1/', added_by: 'ann', batch_id: 1 },
      { prefix: 'gs://b/e2/', added_by: 'bo', batch_id: 2 },
      { prefix: 'gs://b/legacy/', added_by: 'cy', batch_id: null },
    ]
    const stats = { 'gs://b/a/': { bytes: 1, objects: 1 }, 'gs://b/legacy/': { bytes: 1, objects: 1 } }
    expect([queuedOf(countedItems(live, new Set(), null)), queuedOf(countedItems(live, new Set(), stats)), queuedOf(countedItems(live, new Set(['gs://b/legacy/']), stats))]).toEqual([
      { items: 4, batches: 2, stagers: ['ann', 'bo', 'cy'] },
      { items: 2, batches: 1, stagers: ['ann', 'cy'] },
      { items: 1, batches: 1, stagers: ['ann'] },
    ])
  })
})

describe('stage replies: compact, phone-width', () => {
  const T = 2 ** 40
  const stats = (sizes: Record<string, number>) => Object.fromEntries(Object.entries(sizes).map(([p, b]) => [p, { bytes: b, objects: b / T * 10 }]))
  // Every prefix in batch 1 unless `batchOf` says otherwise.
  const reply = (o: Partial<Parameters<typeof stageReply>[0]>) => stageReply({ planId: 1, replyId: 3, by: 'Ann Bee', batches: 1, prefixes: [], stats: null, note: null, siteUrl: 'https://s', ...o, batchOf: { ...Object.fromEntries((o.prefixes ?? []).map(p => [p, 1])), ...o.batchOf } })
  const lines = (m: { blocks: unknown[] }) => (m.blocks[0] as { text: { text: string } }).text.text.split('\n')
  const buttons = (m: { blocks: unknown[] }) => (m.blocks[1] as { elements: { action_id: string; value?: string; text: { text: string } }[] }).elements.map(e => [e.action_id, e.value ?? null, e.text.text])

  it('a few prefixes: themselves, by size; the latest note, on one line', () => {
    const ps = ['gs://b/x/', 'gs://b/y/']
    const m = reply({ prefixes: ps, stats: stats({ 'gs://b/x/': T, 'gs://b/y/': 3 * T }), note: 'old\nruns' })
    expect([m.text, lines(m), buttons(m)]).toEqual([
      'Ann Bee staged 2 paths · 4.0 TiB',
      [':wastebasket: *Ann Bee* staged 2 paths · *4.0 TiB* · 40 objects', '`b/y/` 3.0 TiB, `b/x/` 1.0 TiB', '> old runs'],
      [['staged_reject', '1:r3', 'Reject batch'], ['staged_open', null, 'View in www']],
    ])
  })
  it('many: grouped by parent folder, the top 3 and how many more; unsized, by count', () => {
    const ps = [...['a', 'b', 'c'].map(s => `gs://b/r1/${s}/`), ...['a', 'b'].map(s => `gs://b/r2/${s}/`), 'gs://b/r3/a/', 'gs://b/r4/a/']
    const m = reply({ prefixes: ps, batches: 4, batchOf: Object.fromEntries(ps.map((p, i) => [p, 1 + (i % 4)])) })
    expect([m.text, lines(m), buttons(m)]).toEqual([
      'Ann Bee staged 7 paths in 4 batches',
      [':wastebasket: *Ann Bee* staged 7 paths in 4 batches', '`b/r1/` (3), `b/r2/` (2), `b/r3/a/`, +1 more'],
      [['staged_reject', '1:r3', 'Reject 4 batches'], ['staged_open', null, 'View in www']],
    ])
  })
  it('counts the batches still holding a path: the title those holding what it shows, the reject those it unstages', () => {
    const T1 = { bytes: T, objects: 10 }
    // Batches 1–3 covered: 1 holds a sized path, 2 one empty at the scan, 3 nothing (absorbed by a later stage).
    const m = reply({ prefixes: ['gs://b/x/', 'gs://b/y/'], batches: 3, batchOf: { 'gs://b/x/': 1, 'gs://b/y/': 2 }, stats: { 'gs://b/x/': T1 } })
    expect([m.text, lines(m), buttons(m)]).toEqual([
      'Ann Bee staged 1 path · 1.0 TiB (+1 empty at scan)',
      [':wastebasket: *Ann Bee* staged 1 path · *1.0 TiB* · 10 objects (+1 empty at scan)', '`b/x/` 1.0 TiB'],
      [['staged_reject', '1:r3', 'Reject 2 batches'], ['staged_open', null, 'View in www']],
    ])
  })
  it('long paths keep the bucket and the last two folders', () => {
    expect([shortPath('gs://marin-us-central2/checkpoints/some-team/llama-8b-tootsie-run-42/step-12000/'), shortPath('gs://b/short/path/')])
      .toEqual(['marin-us-central2/…/llama-8b-tootsie-run-42/step-12000/', 'b/short/path/'])
    expect(topFolders(['gs://b/x/'], { 'gs://b/x/': { bytes: 0, objects: 0 } })).toBe('`b/x/` 0 B')
  })
  it('sibling paths under a shared folder: the folder once, then each from there, so long run names still tell them apart', () => {
    const iso = 'gs://marin-us-central2/checkpoints/isoflop'
    const r1 = `${iso}/isoflop-9e+19-d2048-L21-B32-nemo-wider-depth-adapt/checkpoints`
    const r2 = `${iso}/isoflop-3e+19-d1792-L18-B16-nemo-wider-depth-adapt/checkpoints`
    const st = { [`${r1}/step-10000/`]: { bytes: 2 ** 40, objects: 1 }, [`${r2}/step-10000/`]: { bytes: 2 ** 41, objects: 1 } }
    expect(topFolders([`${r1}/step-10000/`, `${r2}/step-10000/`], st)).toBe(
      'in `marin-us-central2/checkpoints/isoflop/`: `isoflop-3e+19-d1792-L18-…er-depth-adapt/…/step-10000/` 2.0 TiB, `isoflop-9e+19-d2048-L21-…er-depth-adapt/…/step-10000/` 1.0 TiB')
  })
  it('a pre-coalescing reply rejects its one batch; nothing left staged → no reject button', () => {
    const legacy = reply({ replyId: null, batchId: 9, prefixes: ['gs://b/x/'] })
    const gone = reply({ batches: 2, prefixes: [] })
    expect([buttons(legacy), [gone.text, lines(gone), buttons(gone)]]).toEqual([
      [['staged_reject', '1:9', 'Reject batch'], ['staged_open', null, 'View in www']],
      ['Ann Bee staged 2 batches: nothing left staged', [':wastebasket: *Ann Bee* staged 2 batches · nothing left staged'], [['staged_open', null, 'View in www']]],
    ])
  })
  it('the old parent, once the plan moved on', () => {
    const m = closedParent(4, 'https://s', 'https://ws.slack.com/archives/C/p1')
    expect([m.text, lines(m)]).toEqual(['Staged plan #4: continued in a new thread', ['*Staged plan #4* · this thread is done; continued in <https://ws.slack.com/archives/C/p1|a new thread>.']])
  })
})

describe('senders', () => {
  it('an event posts as the person (their Slack avatar), else their local part with a generic icon', () => {
    expect([
      personSender('a.b@example.org', { name: 'Ann Bee', image: 'https://img/a.png' }, 'staged'),
      personSender('c.d@example.org', undefined, 'staged'),
      PLAN_SENDER,
    ]).toEqual([
      { username: 'Ann Bee · staged', icon_url: 'https://img/a.png' },
      { username: 'c.d · staged', icon_emoji: ':bust_in_silhouette:' },
      { username: 'Staged deletions', icon_emoji: ':wastebasket:' },
    ])
  })
})

describe('D1', () => {
  describe('the plan card', () => {
    const base = { planId: 7, siteUrl: 'https://site.example.org', items: 3, batches: 2, stagers: [] as string[], digest: 'D1', actions: true, closed: false, runs: [] as RunRow[] }
    const types = (blocks: unknown[]) => blocks.map(b => (b as { type: string }).type)
    it('an image block last, only with an image and items', () => {
      const v = { ...base, image: 'https://site.example.org/og/staged.png?v=abc&sig=x' }
      expect([types(renderParent(v).blocks), renderParent(v).blocks[2], types(renderParent({ ...v, items: 0 }).blocks), types(renderParent(base).blocks)]).toEqual([
        ['section', 'actions', 'image'],
        { type: 'image', image_url: 'https://site.example.org/og/staged.png?v=abc&sig=x', alt_text: 'Plan #7: the staged prefixes as a treemap, coloured by owner' },
        ['section', 'actions'],
        ['section', 'actions'],
      ])
    })
    it('no card without cards on, nor for a non-https origin (Slack fetches it); with them, a full card backed by a fresh `og_tokens` row (cw `0010`)', async () => {
      const { db, raw } = await sqliteD1()
      const off = await stagedCardUrl({ SESSION_SECRET: 's3cret' }, db, 'https://site.example.org', 'abcdef0123456789', 1790000000)
      const local = await stagedCardUrl({ OG_CARDS: '1', SESSION_SECRET: 's3cret' }, db, 'http://localhost:3254', 'abcdef0123456789', 1790000000)
      const on = await stagedCardUrl({ OG_CARDS: '1', SESSION_SECRET: 's3cret' }, db, 'https://site.example.org', 'abcdef0123456789', 1790000000)
      const rows = raw.prepare('SELECT token, kind, view, page, minted_by, minted_ts, exp_day FROM og_tokens').all() as { token: string }[]
      expect([off, local, on?.replace(/t=[a-z0-9]{12}&/, 't=<token>&').replace(/sig=\w+$/, 'sig=<sig>'), rows.map(r => ({ ...r, token: r.token.length }))]).toEqual([
        null,
        null,
        'https://site.example.org/og/staged.png?t=<token>&v=abcdef01&sig=<sig>',
        [{ token: 12, kind: 'staged', view: '', page: '/staged', minted_by: 'slack:staged', minted_ts: 1790000000, exp_day: 293 }],
      ])
      expect(on).toContain(`t=${rows[0].token}&`)
    })
  })

  it('an emptied queue claims no deletions: XML replies can\'t prove them, and neither can a run with skips', () => {
    expect(QUEUE_EMPTIED).toBe(':checkered_flag: Queue emptied: nothing staged here is left to delete (each prefix had a real run recorded, or was empty at the scan).')
  })
  describe('queueWasEmpty: a stage into an emptied queue starts a new thread', () => {
    const items = ['gs://b/a/', 'gs://b/c/', 'gs://b/new/']
    const fresh = new Set(['gs://b/new/'])
    it('every older item deleted by a real run, or empty (or absent) at the scan → empty', () => {
      expect([
        queueWasEmpty(items, fresh, new Set(['gs://b/a/', 'gs://b/c/']), null),
        queueWasEmpty(items, fresh, new Set(['gs://b/a/']), { 'gs://b/c/': 0 }),
        queueWasEmpty(['gs://b/new/'], fresh, new Set(), null),
        queueWasEmpty(items, fresh, new Set(['gs://b/a/']), {}),
      ]).toEqual([true, true, true, true])
    })
    it('any older item still holding bytes, or unsized and not deleted → not empty', () => {
      expect([
        queueWasEmpty(items, fresh, new Set(['gs://b/a/']), { 'gs://b/c/': 5 }),
        queueWasEmpty(items, fresh, new Set(['gs://b/a/']), null),
      ]).toEqual([false, false])
    })
  })
})

describe('an undo a run recorded, where soft delete is off now', () => {
  const base = { planId: 7, siteUrl: 'https://s', items: 1, batches: 1, stagers: [], digest: 'D1', actions: false, closed: false }
  const real = run({ mode: 'real', run_id: 'r1', undo_deadline: 1_790_604_800 })
  const off = { buckets: ['b1'], asOf: '2026-11-05' }
  const ATTEMPTED: RunOutcome = { attempted: true, skipped: null, notSwept: [] }
  const line = (undoUnavailable?: { buckets: string[]; asOf: string } | null, lastOutcome: RunOutcome | null = ATTEMPTED, now = 1_790_000_000, undoUnseen?: boolean) =>
    (renderParent({ ...base, runs: [real], lastOutcome, undoUnavailable, undoUnseen, now }).blocks as { text?: { text?: string } }[])
      .flatMap(b => (b.text?.text ?? '').split('\n')).find(l => /^(Last real run|Real run)/.test(l))

  it('the parent\'s last-real-run line says so, with the bucket and the snapshot\'s date, in place of a deadline', () => {
    expect(line(off)).toBe('Last real run attempted 2.0 TiB / 1,234 objects; undo unavailable: soft delete is off on b1 (as of 2026-11-05).')
  })
  it('mixed buckets: the ones that are off', () => {
    expect(line({ buckets: ['b1', 'b3'], asOf: '2026-11-05' })).toBe('Last real run attempted 2.0 TiB / 1,234 objects; undo unavailable: soft delete is off on b1, b3 (as of 2026-11-05).')
  })
  it('nothing says whether its buckets\' soft delete is still on: the deadline, as depending on it; never "unavailable"', () => {
    expect([line(null, ATTEMPTED, 1_790_000_000, true), line(off, ATTEMPTED, 1_790_000_000, true)]).toEqual([
      "Last real run attempted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z if the buckets' soft delete is still on (this site can't see it).",
      'Last real run attempted 2.0 TiB / 1,234 objects; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
    ])
  })
  it('its buckets on as of the reading, or another executor (nothing passed): the deadline, as recorded', () => {
    expect([line(null), line(undefined)]).toEqual([
      'Last real run attempted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.',
      'Last real run attempted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.',
    ])
  })
  it('keeps what else the line says: a lower bound, the skips', () => {
    expect(line(off, { attempted: true, skipped: { bytes: 3 * 1024 ** 3, objects: 5678 }, notSwept: [], totals: 'lower-bound' }))
      .toBe('Last real run attempted at least 2.0 TiB / 1,234 objects; undo unavailable: soft delete is off on b1 (as of 2026-11-05). Skipped, no generation: *3.0 GiB* / 5,678 objects left in place.')
  })
  it('a run past its recorded window is history either way', () => {
    expect(line(off, ATTEMPTED, 1_790_604_800)).toBeUndefined()
  })
  it('a run\'s finish says so too, and a killed one drops the word on what undo restores', () => {
    expect([
      runEvent(real, 'finished', { undoUnavailable: off }),
      runEvent(real, 'finished', { outcome: { attempted: false, skipped: null, notSwept: [], unread: true }, undoUnavailable: off }),
      runEvent({ ...real, plan_digest: '' }, 'failed', { outcome: { ...ATTEMPTED, totals: 'exact' }, undoUnavailable: off }),
      runEvent({ ...real, plan_digest: '' }, 'failed', { outcome: { ...ATTEMPTED, totals: 'lower-bound' }, undoUnavailable: off }),
      runEvent(real, 'finished', { undoUnavailable: null }),
    ]).toEqual([
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
      ':warning: Real deletion `r1` finished, totals unread: recorded *2.0 TiB* / 1,234 objects; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
      ':warning: *Real deletion* `r1` ended unsuccessfully: attempted *2.0 TiB* / 1,234 objects; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
      ':warning: *Real deletion* `r1` ended before recording its end: attempted at least *2.0 TiB* / 1,234 objects, from its progress files, which can lag what it did; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).',
    ])
  })
  it('a run that recorded no window keeps its own reason', () => {
    expect(runEvent({ ...real, undo_deadline: null, soft_delete_days: 0 }, 'finished', { undoUnavailable: off }))
      .toBe(':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; no undo (soft delete off).')
  })
})

describe('a last real run that was undone', () => {
  const base = { planId: 7, siteUrl: 'https://s', items: 1, batches: 1, stagers: [], digest: 'D1', actions: false, closed: false }
  const real = run({ mode: 'real', run_id: 'r1', undo_deadline: 1_790_604_800 })
  const off = { buckets: ['b1'], asOf: '2026-11-05' }
  const line = (undo_state: string, o: { undoUnavailable?: typeof off | null; undoUnseen?: boolean; now?: number } = {}) =>
    (renderParent({ ...base, runs: [{ ...real, undo_state }], now: 1_790_000_000, ...o }).blocks as { text?: { text?: string } }[])
      .flatMap(b => (b.text?.text ?? '').split('\n')).find(l => /^(Last real run|Real run)/.test(l))

  it('the parent says it was undone, not that it is undoable', () => {
    expect(line('full')).toBe('Last real run deleted 2.0 TiB / 1,234 objects, since undone.')
  })
  it('an undone run has no undo left to qualify: no deadline, no "unavailable", no "if soft delete is still on"', () => {
    expect([line('full', { undoUnavailable: off }), line('full', { undoUnseen: true })]).toEqual([
      'Last real run deleted 2.0 TiB / 1,234 objects, since undone.',
      'Last real run deleted 2.0 TiB / 1,234 objects, since undone.',
    ])
  })
  it('a partly undone run says so, and what the rest has: its deadline, that it depends on soft delete, or that it is unavailable', () => {
    expect([line('partial'), line('partial', { undoUnseen: true }), line('partial', { undoUnavailable: off })]).toEqual([
      'Last real run deleted 2.0 TiB / 1,234 objects, since partly undone; the rest undoable until 2026-09-28 14:13Z.',
      "Last real run deleted 2.0 TiB / 1,234 objects, since partly undone; the rest undoable until 2026-09-28 14:13Z if the buckets' soft delete is still on (this site can't see it).",
      'Last real run deleted 2.0 TiB / 1,234 objects, since partly undone; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
    ])
  })
  it('nothing undone: the deadline, as before', () => {
    expect(line('none')).toBe('Last real run deleted 2.0 TiB / 1,234 objects, undoable until 2026-09-28 14:13Z.')
  })
  it('past its recorded window it is history, undone or not', () => {
    expect([line('full', { now: 1_790_604_800 }), line('partial', { now: 1_790_604_800 })]).toEqual([undefined, undefined])
  })
  it('a run\'s own event, rendered from a row an undo already reached, says the same', () => {
    expect([
      runEvent({ ...real, undo_state: 'full' }, 'finished'),
      runEvent({ ...real, undo_state: 'full' }, 'finished', { undoUnavailable: off }),
      runEvent({ ...real, undo_state: 'partial' }, 'finished'),
      runEvent({ ...real, undo_state: 'partial' }, 'finished', { undoUnavailable: off }),
      runEvent({ ...real, undo_state: 'none' }, 'finished'),
      runEvent({ ...real, undo_state: 'full', plan_digest: '' }, 'failed', { outcome: { attempted: true, skipped: null, notSwept: [], totals: 'exact' } }),
    ]).toEqual([
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; since undone.',
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; since undone.',
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; since partly undone, the rest undoable until 2026-09-28 14:13Z (www).',
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; since partly undone; undo unavailable: soft delete is off on b1 (as of 2026-11-05).',
      ':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).',
      ':warning: *Real deletion* `r1` ended unsuccessfully: attempted *2.0 TiB* / 1,234 objects; since undone.',
    ])
  })
  it('an undone run that recorded no window (an executor whose undo needs none) says undone, not nothing', () => {
    expect(runEvent({ ...real, undo_deadline: null, undo_state: 'full' }, 'finished')).toBe(':white_check_mark: Real deletion `r1` finished: deleted *2.0 TiB* / 1,234 objects; since undone.')
  })
})

it('explains an empty completed review without claiming soft delete is off', () => {
  expect(runEvent(run({ run_id: 'empty', mode: 'real', deleted_bytes: 0, deleted_objects: 0, undo_deadline: null, soft_delete_days: 0 }), 'finished', {
    outcome: { attempted: true, skipped: null, notSwept: [], preserved: { bytes: 12, objects: 3 } },
  })).toBe(':warning: Real deletion `empty` finished: attempted *0 B* / 0 objects; nothing to undo (no objects processed). Preserved since staging: *12 B* / 3 newer or changed objects.')
})
