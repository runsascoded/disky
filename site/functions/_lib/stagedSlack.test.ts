import { describe, expect, it } from 'vitest'
import { closedParent, PLAN_SENDER, queueWasEmpty, fmtBytes, personSender, renderParent, runEvent, runUrl, shortPath, sizeOf, stageReply, stagedCardUrl, topFolders, type RunRow } from './stagedSlack.js'
import { sqliteD1 } from './testD1.js'

const run = (o: Partial<RunRow>): RunRow => ({
  run_id: 'cw-sweep-dry-1', mode: 'dry', scan: '2026-09-28T1201', actor: 'ann@openathena.ai', started_ts: 100,
  finished_ts: 200, deleted_bytes: 2 * 1024 ** 4, deleted_objects: 1234, skipped_gone: 0, skipped_overwritten: 0,
  plan_digest: 'D1', undo_deadline: null, ...o,
})

const ids = (blocks: unknown[]): string[] =>
  ((blocks[2] as { elements: { action_id: string }[] }).elements).map(e => e.action_id)

describe('renderParent', () => {
  const base = { planId: 7, siteUrl: 'https://cw-s3.oa.dev', items: 3, batches: 2, stagers: ['ann@openathena.ai', 'bo@coreweave.com'], digest: 'D1', actions: true, closed: false }

  it('offers Delete for real only once a matching dry-run has finished', () => {
    expect(ids(renderParent({ ...base, runs: [] }).blocks)).toEqual(['staged_open', 'staged_dry'])
    expect(ids(renderParent({ ...base, runs: [run({ plan_digest: 'OLD' })] }).blocks)).toEqual(['staged_open', 'staged_dry'])
    expect(ids(renderParent({ ...base, runs: [run({})] }).blocks)).toEqual(['staged_open', 'staged_dry', 'staged_real'])
  })

  it('shows only the www link when dispatch is not wired, the plan is closed, or empty', () => {
    expect(ids(renderParent({ ...base, runs: [run({})], actions: false }).blocks)).toEqual(['staged_open'])
    expect(ids(renderParent({ ...base, runs: [run({})], closed: true }).blocks)).toEqual(['staged_open'])
    expect(ids(renderParent({ ...base, runs: [], items: 0 }).blocks)).toEqual(['staged_open'])
  })

  it('says whether the latest dry-run still matches', () => {
    const txt = (runs: RunRow[]): string => (renderParent({ ...base, runs }).blocks[1] as { text: { text: string } }).text.text
    expect(txt([])).toBe('No dry-run yet.\n_Delete for real_ appears after a finished dry-run of the current set (no dry-run of this plan yet).')
    expect(txt([run({})])).toBe('Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — matches the current plan.')
    expect(txt([run({ plan_digest: 'OLD' })])).toBe('Latest dry-run would delete *2.0 TiB* / 1,234 objects (scan 2026-09-28T1201) — *stale*: the plan changed since.\n_Delete for real_ appears after a finished dry-run of the current set (the plan changed since the last dry-run; dry-run it again).')
    expect(txt([run({ plan_digest: '', deleted_bytes: 0, deleted_objects: 0 })])).toBe('Latest dry-run (`cw-sweep-dry-1`) ended without a result.\n_Delete for real_ appears after a finished dry-run of the current set (the plan changed since the last dry-run; dry-run it again).')
  })

  it('titles by what is still queued (deleted items drop out), the gate by the whole plan', () => {
    const head = (v: Partial<Parameters<typeof renderParent>[0]>): string => (renderParent({ ...base, runs: [], ...v }).blocks[0] as { text: { text: string } }).text.text
    expect([
      head({}),
      head({ queued: { items: 1, batches: 1, stagers: ['bo@coreweave.com'] } }),
    ]).toEqual([
      '*Staged for deletion* · plan #7 · 3 prefixes in 2 batches\nstaged by ann, bo',
      '*Staged for deletion* · plan #7 · 1 prefix in 1 batch\nstaged by bo',
    ])
  })

  it('puts the dry-run numbers and the recoverability in the real-delete confirm', () => {
    const real = (renderParent({ ...base, runs: [run({})] }).blocks[2] as { elements: Record<string, unknown>[] }).elements[2]
    expect(real.value).toBe('7:D1')
    expect((real.confirm as { text: { text: string } }).text.text).toBe('Deletes the 3 staged prefixes: 2.0 TiB / 1,234 objects per the dry-run on scan 2026-09-28T1201. Recoverable for 7 days (undo in www).')
  })
})

describe('run events', () => {
  it('reads as a sentence per phase', () => {
    const site = 'https://cw-s3.oa.dev'
    const link = '<https://cw-s3.oa.dev/staged?run=cw-sweep-dry-1|cw-sweep-dry-1>'
    expect(runEvent(run({ finished_ts: null }), 'dispatched', { via: 'Slack' })).toBe(':test_tube: Dry-run dispatched by ann via Slack on scan 2026-09-28T1201 (`cw-sweep-dry-1`)')
    expect(runEvent(run({ finished_ts: null }), 'dispatched', { via: 'www', names: { 'ann@openathena.ai': 'Ann Example' }, siteUrl: site }))
      .toBe(`:test_tube: Dry-run dispatched by Ann Example via www on scan 2026-09-28T1201 (${link})`)
    expect(runEvent(run({ skipped_gone: 2 }), 'finished', { siteUrl: site })).toBe(`:test_tube: Dry-run ${link} finished: would delete *2.0 TiB* / 1,234 objects (gone since scan: 2, overwritten: 0).`)
    expect(runEvent(run({ mode: 'real', run_id: 'cw-sweep-real-1', undo_deadline: 1_790_604_800 }), 'finished')).toBe(':white_check_mark: Real deletion `cw-sweep-real-1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
    expect(runEvent(run({ plan_digest: '' }), 'failed', { siteUrl: site })).toBe(`:x: Dry-run ${link} ended without a result (its Batch job stopped before the run summary); check its logs in www.`)
    expect(runUrl(site, '2026-09-28-p1/20260928T120000Z')).toBe('https://cw-s3.oa.dev/staged?run=2026-09-28-p1%2F20260928T120000Z')
    expect([fmtBytes(0), fmtBytes(1536), fmtBytes(3 * 1024 ** 3)]).toEqual(['0 B', '1.5 KiB', '3.0 GiB'])
  })
})

describe('names and sizes (plain text, never a mention)', () => {
  const base = { planId: 7, siteUrl: 'https://cw-s3.oa.dev', items: 3, batches: 2, stagers: [] as string[], digest: 'D1', actions: true, closed: false, runs: [] as RunRow[] }
  const size = { scan: '2026-10-02', b: 51 * 2 ** 40, o: 179327698, empty: 5, owners: [{ label: 'Grace Hopper', b: 16 * 2 ** 40 }, { label: 'hedy-lamarr', b: 6 * 2 ** 40 }] }
  it('the parent names stagers by Slack name (else the local part) and carries the size line', () => {
    const v = { ...base, stagers: ['A.B@x.org', 'c.d@x.org'], names: { 'a.b@x.org': 'Ann Bee' }, size }
    expect((renderParent(v).blocks[0] as { text: { text: string } }).text.text.split('\n')).toEqual([
      `*Staged for deletion* · plan #${base.planId} · ${base.items} prefixes in ${base.batches} batches`,
      'staged by Ann Bee, c.d',
      '*51.0 TiB* · 179,327,698 objects at scan 2026-10-02 · 5 empty · owners: Grace Hopper 16.0 TiB, hedy-lamarr 6.0 TiB',
    ])
  })
  it('sizeOf: totals over every prefix, absent or zero = empty, owners by attributed bytes', () => {
    const at = { scan: 'S', stats: { 'gs://b/a/': { b: 5, o: 2, us: [['u1', 3], ['u2', 2]] as [string, number][] }, 'gs://b/c/': { b: 4, o: 1, us: [['u2', 4]] as [string, number][] }, 'gs://b/z/': { b: 0, o: 0 } } }
    expect(sizeOf(at, ['gs://b/a/', 'gs://b/c/', 'gs://b/z/', 'gs://b/gone/'], u => u.toUpperCase(), 1)).toEqual({ scan: 'S', b: 9, o: 3, empty: 2, owners: [{ label: 'U2', b: 6 }] })
  })
})

describe('stage replies: compact, phone-width', () => {
  const T = 2 ** 40
  const stats = (sizes: Record<string, number>) => Object.fromEntries(Object.entries(sizes).map(([p, b]) => [p, { b, o: b / T * 10 }]))
  const reply = (o: Partial<Parameters<typeof stageReply>[0]>) => stageReply({ planId: 1, replyId: 3, by: 'Ann Bee', batches: 1, prefixes: [], stats: null, note: null, siteUrl: 'https://s', ...o })
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
    const m = reply({ prefixes: ps, batches: 4 })
    expect([m.text, lines(m), buttons(m)]).toEqual([
      'Ann Bee staged 7 paths in 4 batches',
      [':wastebasket: *Ann Bee* staged 7 paths in 4 batches', '`b/r1/` (3), `b/r2/` (2), `b/r3/a/`, +1 more'],
      [['staged_reject', '1:r3', 'Reject 4 batches'], ['staged_open', null, 'View in www']],
    ])
  })
  it('long paths keep the bucket and the last two folders', () => {
    expect([shortPath('gs://marin-us-central2/checkpoints/some-team/llama-8b-tootsie-run-42/step-12000/'), shortPath('gs://b/short/path/')])
      .toEqual(['marin-us-central2/…/llama-8b-tootsie-run-42/step-12000/', 'b/short/path/'])
    expect(topFolders(['gs://b/x/'], { 'gs://b/x/': { b: 0, o: 0 } })).toBe('`b/x/` 0 B')
  })
  it('sibling paths under a shared folder: the folder once, then each from there, so long run names still tell them apart', () => {
    const iso = 'gs://marin-us-central2/checkpoints/isoflop'
    const r1 = `${iso}/isoflop-9e+19-d2048-L21-B32-nemo-wider-depth-adapt/checkpoints`
    const r2 = `${iso}/isoflop-3e+19-d1792-L18-B16-nemo-wider-depth-adapt/checkpoints`
    const st = { [`${r1}/step-10000/`]: { b: 2 ** 40, o: 1 }, [`${r2}/step-10000/`]: { b: 2 ** 41, o: 1 } }
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
      personSender('a.b@x.org', { name: 'Ann Bee', image: 'https://img/a.png' }, 'staged'),
      personSender('c.d@x.org', undefined, 'staged'),
      PLAN_SENDER,
    ]).toEqual([
      { username: 'Ann Bee · staged', icon_url: 'https://img/a.png' },
      { username: 'c.d · staged', icon_emoji: ':bust_in_silhouette:' },
      { username: 'Staged deletions', icon_emoji: ':wastebasket:' },
    ])
  })
})

describe('the plan card', () => {
  const base = { planId: 7, siteUrl: 'https://site.example.org', items: 3, batches: 2, stagers: [] as string[], digest: 'D1', actions: true, closed: false, runs: [] as RunRow[] }
  const types = (blocks: unknown[]) => blocks.map(b => (b as { type: string }).type)
  it('an image block last, only with an image and items', () => {
    const v = { ...base, image: 'https://site.example.org/og/staged.png?v=abc&sig=x' }
    expect([types(renderParent(v).blocks), renderParent(v).blocks[3], types(renderParent({ ...v, items: 0 }).blocks), types(renderParent(base).blocks)]).toEqual([
      ['section', 'section', 'actions', 'image'],
      { type: 'image', image_url: 'https://site.example.org/og/staged.png?v=abc&sig=x', alt_text: 'Plan #7: the staged prefixes as a treemap, coloured by owner' },
      ['section', 'section', 'actions'],
      ['section', 'section', 'actions'],
    ])
  })
  it('no card without cards on, nor for a non-https origin (Slack fetches it); with them, a full card backed by a fresh `og_tokens` row (cw `0010`)', async () => {
    const { db, raw } = await sqliteD1('cw')
    const off = await stagedCardUrl({ SESSION_SECRET: 's3cret' }, db, 'https://site.example.org', 'abcdef0123456789', 1790000000)
    const local = await stagedCardUrl({ OG_CARDS: '1', SESSION_SECRET: 's3cret' }, db, 'http://localhost:3254', 'abcdef0123456789', 1790000000)
    const on = await stagedCardUrl({ OG_CARDS: '1', SESSION_SECRET: 's3cret' }, db, 'https://site.example.org', 'abcdef0123456789', 1790000000)
    const rows = raw.prepare('SELECT token, kind, view, page, minted_by, minted_ts, exp_day FROM og_tokens').all() as { token: string }[]
    expect([off, local, on?.replace(/t=\w{10}&/, 't=<token>&').replace(/sig=\w+$/, 'sig=<sig>'), rows.map(r => ({ ...r, token: r.token.length }))]).toEqual([
      null,
      null,
      'https://site.example.org/og/staged.png?t=<token>&v=abcdef01&sig=<sig>',
      [{ token: 10, kind: 'staged', view: '', page: '/staged', minted_by: 'slack:staged', minted_ts: 1790000000, exp_day: 293 }],
    ])
    expect(on).toContain(`t=${rows[0].token}&`)
  })
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
