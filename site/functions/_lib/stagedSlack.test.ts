import { describe, expect, it } from 'vitest'
import { PLAN_SENDER, fmtBytes, nameSlug, personSender, renderParent, runEvent, runUrl, stageEvent, stagedCardUrl, type RunRow } from './stagedSlack.js'
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
    expect(runEvent(run({ finished_ts: null }), 'dispatched', { via: 'www', mentions: { 'ann@openathena.ai': '<@U1>' }, siteUrl: site }))
      .toBe(`:test_tube: Dry-run dispatched by <@U1> via www on scan 2026-09-28T1201 (${link})`)
    expect(runEvent(run({ skipped_gone: 2 }), 'finished', { siteUrl: site })).toBe(`:test_tube: Dry-run ${link} finished: would delete *2.0 TiB* / 1,234 objects (gone since scan: 2, overwritten: 0).`)
    expect(runEvent(run({ mode: 'real', run_id: 'cw-sweep-real-1', undo_deadline: 1_790_604_800 }), 'finished')).toBe(':white_check_mark: Real deletion `cw-sweep-real-1` finished: deleted *2.0 TiB* / 1,234 objects; undoable until 2026-09-28 14:13Z (www).')
    expect(runEvent(run({ plan_digest: '' }), 'failed', { siteUrl: site })).toBe(`:x: Dry-run ${link} ended without a result (its Batch job stopped before the run summary); check its logs in www.`)
    expect(runUrl(site, '2026-09-28-p1/20260928T120000Z')).toBe('https://cw-s3.oa.dev/staged?run=2026-09-28-p1%2F20260928T120000Z')
    expect([fmtBytes(0), fmtBytes(1536), fmtBytes(3 * 1024 ** 3)]).toEqual(['0 B', '1.5 KiB', '3.0 GiB'])
  })
})

describe('mentions and sizes', () => {
  const base = { planId: 7, siteUrl: 'https://cw-s3.oa.dev', items: 3, batches: 2, stagers: [] as string[], digest: 'D1', actions: true, closed: false, runs: [] as RunRow[] }
  const size = { scan: '2026-10-02', b: 51 * 2 ** 40, o: 179327698, empty: 5, owners: [{ label: '<@U1>', b: 16 * 2 ** 40 }, { label: 'hedy-lamarr', b: 6 * 2 ** 40 }] }
  it('the parent names stagers by mention (else the local part) and carries the size line', () => {
    const v = { ...base, stagers: ['a.b@x.org', 'c.d@x.org'], mentions: { 'a.b@x.org': '<@UA>' }, size }
    expect((renderParent(v).blocks[0] as { text: { text: string } }).text.text.split('\n')).toEqual([
      `*Staged for deletion* · plan #${base.planId} · ${base.items} prefixes in ${base.batches} batches`,
      'staged by <@UA>, c.d',
      '*51.0 TiB* · 179,327,698 objects at scan 2026-10-02 · 5 empty · owners: <@U1> 16.0 TiB, hedy-lamarr 6.0 TiB',
    ])
  })
  it('a stage reply: mention, size line, then the note and prefixes', () => {
    const e = stageEvent({ planId: 1, batchId: 2, by: 'a.b@x.org', prefixes: ['gs://b/x/'], covered: 0, note: 'old runs', siteUrl: 'https://s', mentions: { 'a.b@x.org': '<@UA>' }, size: { ...size, empty: 0, owners: [] } })
    expect([e.text, (e.blocks[0] as { text: { text: string } }).text.text.split('\n')]).toEqual(['<@UA> staged 1 prefix', [
      ':wastebasket: *<@UA> staged 1 prefix*',
      '*51.0 TiB* · 179,327,698 objects at scan 2026-10-02',
      '> old runs',
      '```b/x/```',
    ]])
  })
})

describe('nameSlug: a Slack name as the canonical owner id', () => {
  it('lowercase, accents folded, other runs to one dash', () => {
    expect(['Grace Hopper', 'Hedy Lamarr', 'Émilie  du Châtelet', ' Alan Turing (he/him) '].map(nameSlug))
      .toEqual(['grace-hopper', 'hedy-lamarr', 'emilie-du-chatelet', 'alan-turing-he-him'])
  })
})

describe('senders', () => {
  it('an event posts as the person (their Slack avatar), else their local part with a generic icon', () => {
    expect([
      personSender('a.b@x.org', { mention: '<@UA>', name: 'Ann Bee', image: 'https://img/a.png' }, 'staged'),
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
  it('no card without cards on; with them, a full card backed by a fresh `og_tokens` row (cw `0010`)', async () => {
    const { db, raw } = await sqliteD1('cw')
    const off = await stagedCardUrl({ SESSION_SECRET: 's3cret' }, db, 'https://site.example.org', 'abcdef0123456789', 1790000000)
    const on = await stagedCardUrl({ OG_CARDS: '1', SESSION_SECRET: 's3cret' }, db, 'https://site.example.org', 'abcdef0123456789', 1790000000)
    const rows = raw.prepare('SELECT token, kind, view, page, minted_by, minted_ts, exp_day FROM og_tokens').all() as { token: string }[]
    expect([off, on?.replace(/t=\w{10}&/, 't=<token>&').replace(/sig=\w+$/, 'sig=<sig>'), rows.map(r => ({ ...r, token: r.token.length }))]).toEqual([
      null,
      'https://site.example.org/og/staged.png?t=<token>&v=abcdef01&sig=<sig>',
      [{ token: 10, kind: 'staged', view: '', page: '/staged', minted_by: 'slack:staged', minted_ts: 1790000000, exp_day: 293 }],
    ])
    expect(on).toContain(`t=${rows[0].token}&`)
  })
})
