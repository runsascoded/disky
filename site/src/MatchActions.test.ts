import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { ActState } from './matchAct'
import type { Targets } from './filterCover'

// The tooltip's content inline (the real one mounts on hover), the trash glyph as text: what's left is each state's exact markup.
vi.mock('./Tooltip', async () => {
  const { createElement: h } = await import('react')
  return { Tooltip: ({ content, children, placement }: { content: ReactNode; children: ReactNode; placement?: string }) => h('span', { className: 'tt', 'data-placement': placement }, children, h('span', { className: 'tt-tip' }, content)) }
})
vi.mock('react-icons/fa6', async () => {
  const { createElement: h } = await import('react')
  return { FaRegTrashCan: () => h('svg', null) }
})
const { RowActsView } = await import('./MatchActions')

const nop = () => {}
const act = { start: async () => {}, confirm: async () => {}, cancel: nop, undo: async () => {}, dismiss: nop, retry: async () => {} }
const t = (o: Partial<Targets>): Targets => ({ items: [], b: 0, o: 0, folders: 0, files: 0, buckets: 0, unknown: 0, ...o })
const render = (state: ActState, o: { warm?: 'cold' | 'warming' | 'ready'; confirmT?: Targets } = {}) => renderToStaticMarkup(createElement(RowActsView, {
  state, confirmT: o.confirmT ?? null, act, warm: o.warm ?? 'cold', canTrash: true, fmtBytes: (b: number) => `${b} B`, scheme: 'gs://',
}))

const TRASH = '<span class="tt" data-placement="right"><button type="button" class="trash" aria-label="trash"><svg></svg></button><span class="tt-tip">Stage this row’s matches for deletion (not the rest of the row; listed when you click) — an admin approves and dispatches from /staged</span></span>'
const box = (inner: string, live: string, attrs = '') => `<span class="mact"${attrs} tabindex="-1">${inner}<span class="act-live" role="status" aria-live="polite">${live}</span></span>`
const X = '<button type="button" class="act x" aria-label="dismiss">×</button>'
const SPIN = '<span class="spin" aria-hidden="true"></span>'
const req = { kind: 'stage' as const }
const OVER = 'Too many matches to act on at once (70,390); narrow the search or open a folder below. Agents can bulk-assign via the API.'

describe('a filtered row\'s controls, per state', () => {
  it('idle: trash, offered at once (no "list" step); the live region empty', () => {
    expect(render({ s: 'idle' })).toBe(box(TRASH, ''))
  })
  it('prefetching (hovered): the same controls, busy', () => {
    expect(render({ s: 'idle' }, { warm: 'warming' })).toBe(box(TRASH, '').replace('<span class="mact"', '<span class="mact warming" aria-busy="true"'))
  })
  it('in progress: a spinner while listing and for one batch; a bar over several batches', () => {
    expect([
      render({ s: 'resolving', req }),
      render({ s: 'sending', req: { kind: 'assign', who: 'you' }, t: t({ items: [{ key: 'gs://b/x/', kind: 'prefix' }], folders: 1 }), batch: 0, batches: 1 }),
      render({ s: 'sending', req, t: t({ folders: 11_000 }), batch: 3, batches: 22 }),
    ]).toEqual([
      box('', `<span class="act-st busy">${SPIN}listing the matches…</span>`),
      box('', `<span class="act-st busy">${SPIN}assigning 1 folder…</span>`),
      box('', '<span class="act-st busy"><span class="act-bar" role="progressbar" aria-label="staging: batches sent" aria-valuemin="0" aria-valuemax="22" aria-valuenow="3"><i style="width:14%"></i></span>staging 3/22 batches</span>'),
    ])
  })
  it('confirm (a large set): what it sends, in how many batches, and what it leaves out', () => {
    const items = Array.from({ length: 1234 }, (_, i) => ({ key: `gs://b/f${i}`, kind: 'object' as const }))
    expect(render({ s: 'confirm', req, got: { items: [], complete: true } }, { confirmT: t({ items, files: 1234, b: 5000, o: 1234, unknown: 2 }) })).toBe(box('',
      '<span class="act-confirm">stage <b>1,234 files</b> (5000 B, 1,234 objects), sent in 3 batches?'
      + '<span class="act-items"><code title="b/f0">b/f0</code><code title="b/f1">b/f1</code><code title="b/f2">b/f2</code><span class="act-more">+1,231 more (1,231 files)</span></span>'
      + '<span class="act-note"> 2 matches couldn’t be checked (file or folder?) and are left out; open their folder to act on them.</span>'
      + '<button type="button" class="act go">confirm</button><button type="button" class="act">cancel</button></span>'))
  })
  it('confirm (a row\'s items beyond its label): the first few named, then the rest counted, folders and files apart', () => {
    const P = 'marin-us-east5/data/hrm_text_split/flan_direct/flan_'
    const f = (i: number) => ({ key: `gs://${P}${i}_rotten_tomatoes_part_00000.parquet`, kind: 'object' as const })
    const nine = [{ key: 'gs://marin-us-east5/tomat/', kind: 'prefix' as const }, ...[0, 1, 2, 3, 4, 5, 6, 7].map(f)]
    const mixed = [...nine.slice(1, 4), { key: 'gs://b/x/', kind: 'prefix' as const }, { key: 'gs://b/y/', kind: 'prefix' as const }, f(9)]
    const confirm = (items: Targets['items'], folders: number) => render({ s: 'confirm', req: { kind: 'assign', who: 'you' }, got: { items: [], complete: true } }, { confirmT: t({ items, folders, files: items.length - folders, b: 9, o: 9 }) })
    const head = '<span class="act-confirm">assign → you <b>'
    const tail = '<button type="button" class="act go">confirm</button><button type="button" class="act">cancel</button></span>'
    const code = (p: string) => `<code title="${p}">${p.length > 60 ? `${p.slice(0, 31)}…${p.slice(-28)}` : p}</code>`
    const flan = (i: number) => `${P}${i}_rotten_tomatoes_part_00000.parquet`
    expect([confirm(nine, 1), confirm(mixed, 2), confirm(nine.slice(0, 2), 1)]).toEqual([
      box('', `${head}1 folder, 8 files</b> (9 B, 9 objects)?<span class="act-items">${code('marin-us-east5/tomat/')}${code(flan(0))}${code(flan(1))}<span class="act-more">+6 more (6 files)</span></span>${tail}`),
      box('', `${head}2 folders, 4 files</b> (9 B, 9 objects)?<span class="act-items">${code(flan(0))}${code(flan(1))}${code(flan(2))}<span class="act-more">+3 more (2 folders, 1 file)</span></span>${tail}`),
      box('', `${head}1 folder, 1 file</b> (9 B, 9 objects)?<span class="act-items">${code('marin-us-east5/tomat/')}${code(flan(0))}</span>${tail}`),
    ])
    // The elided path, spelled out: its head and its file name.
    expect(code(flan(0))).toBe(`<code title="${flan(0)}">marin-us-east5/data/hrm_text_sp…_tomatoes_part_00000.parquet</code>`)
  })
  it('accepted: what landed, an undo when there is one, a dismiss', () => {
    expect([
      render({ s: 'done', req: { kind: 'assign', who: 'you' }, text: 'assigned 1,234 files → you' }),
      render({ s: 'done', req, text: 'staged 3 folders', undo: { plan_id: 4, keys: ['gs://b/x/'] } }),
    ]).toEqual([
      box('', `<span class="act-st ok">✓ assigned 1,234 files → you</span>${X}`),
      box('', `<span class="act-st ok">✓ staged 3 folders</span><a class="act" href="/staged">review staged</a><button type="button" class="act">undo</button>${X}`),
    ])
  })
  it('over the cap after resolving: the reason, muted, in the controls\' place (no red)', () => {
    expect(render({ s: 'muted', reason: OVER })).toBe(box('',
      `<span class="tt"><span class="act-st muted" tabindex="0">${OVER}</span><span class="tt-tip"><span class="bb-tip">${OVER}</span></span></span>${X}`))
  })
  it('failed (a 5xx, the network): red, with a retry', () => {
    expect(render({ s: 'failed', req: { kind: 'assign' }, msg: 'filter-cover: 503' })).toBe(box('',
      `<span class="act-st err">Can’t assign: filter-cover: 503</span><button type="button" class="act">retry</button>${X}`))
  })
})
