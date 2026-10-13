import { createElement, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { applyToggle, BulkBar, caveats } from './BulkBar'
import { ACT_BLOCKED_LABEL, actBlock } from './matchAct'
import { overCapReason } from '../functions/_lib/cover'
import { actionTargets, groupItems, type FilterCover, targetsText } from './filterCover'

vi.mock('./owners', () => ({ useOwnerMutations: () => ({ post: { error: null } }), getCurrentScan: () => undefined, ownerPost: () => ({}) }))
vi.mock('./plans', () => ({ call: async () => ({}) }))
vi.mock('@tanstack/react-query', async orig => ({ ...await orig<object>(), useQueryClient: () => ({ invalidateQueries: () => {} }) }))
vi.mock('./UserChip', () => ({ allUsers: () => [] }))
// The tooltip's content rendered inline (the real one mounts it on hover), so the markup pins it.
vi.mock('./Tooltip', async () => {
  const { createElement: h } = await import('react')
  return { Tooltip: ({ content, children }: { content: ReactNode; children: ReactNode }) => h('span', { className: 'tt' }, children, h('span', { className: 'tt-tip' }, content)) }
})
vi.mock('./units', () => ({ useUnits: () => ({ fmtBytes: (b: number) => `${b} B` }) }))

// The Adam_Carolla `tomat` shape: every match a file directly in one folder that also holds other files (no
// collapse), plus a folder elsewhere every file of which matches (collapsed to one prefix).
const SHOW = 'marin-us-central1/podcast_audio_top1000_60s_clips/Adam_Carolla_Show'
const clips = Array.from({ length: 3 }, (_, i) => ({ path: `${SHOW}/ACS_Rotten_Tomatoes_chunk00${i}.mp3`, kind: 'file' as const, b: 100, o: 1, roots: 1 }))
const cover = (o: Partial<FilterCover> = {}): FilterCover => ({
  date: '2026-10-09', path: '', q: 'tomat', complete: true, looked: 2, unchecked: 0,
  roots: { n: 5, b: 300 + 50, o: 5 },
  items: [{ path: 'marin-eu-west4/tomat', kind: 'dir', b: 50, o: 2, roots: 2 }, ...clips],
  ...o,
})
const resolve = async () => cover()
const render = (c: FilterCover | undefined, opts: { canAssign?: boolean; canStage?: boolean; loading?: boolean; blocked?: string | null } = {}) =>
  renderToStaticMarkup(createElement(BulkBar, { cover: c, loading: opts.loading, resolve, scheme: 'gs://', query: 'tomat', canAssign: opts.canAssign ?? true, canStage: opts.canStage ?? true, blocked: opts.blocked }))
const text = (html: string) => html.replace(/<[^>]+>/g, '|').split('|').map(s => s.trim()).filter(Boolean)

describe('the bulk bar lists the matches as prefixes, grouped by folder, for review', () => {
  it('offers assign + stage, then summarizes and groups heaviest folder first (no cap, no jargon)', () => {
    expect(text(render(cover()))).toEqual([
      'assign to me', 'stage for deletion',
      '5 matches → 1 folder, 3 files · 350 B',
      'review',
      'Each line is a folder whose every file matches (sent as that folder), or a single matching file (sent as exactly that file — never its neighbours). Untick false positives — a whole folder’s worth, or one at a time.',
      'all', 'none',
      `${SHOW}/`, '3/3 · 300 B',
      'ACS_Rotten_Tomatoes_chunk000.mp3', '100 B',
      'ACS_Rotten_Tomatoes_chunk001.mp3', '100 B',
      'ACS_Rotten_Tomatoes_chunk002.mp3', '100 B',
      'marin-eu-west4/', '50 B',
      'tomat/', '2 files · 50 B',
    ])
  })
  it('nothing to offer a viewer who can neither assign nor stage', () => {
    expect(render(cover(), { canAssign: false, canStage: false })).toBe('')
  })
})

// Offered at once, whatever the cover says: nothing is disabled ahead of the click (over the cap, the click
// says so, muted, in the button's place — `MatchActions.test.ts`), and a hover's failure shows nothing.
describe('offered at once: idle, prefetching, listed, over the cap', () => {
  const ACTS = '<span class="mact bb-acts" tabindex="-1"><button type="button" class="act assign">assign to me</button><input list="bb-assign-users" placeholder="you" size="7" aria-label="Assign matches to user" value=""/><datalist id="bb-assign-users"></datalist><button type="button" class="act stage">stage for deletion</button><span class="act-live" role="status" aria-live="polite"></span></span><input placeholder="memo" size="10" aria-label="Bulk memo" value=""/>'
  it('idle (nothing listed yet): the actions, enabled; no count', () => {
    expect(render(undefined)).toBe(`<span class="bulkbar">${ACTS}</span>`)
  })
  it('prefetching (a hover started the listing): the same actions, and a quiet note', () => {
    expect(render(undefined, { loading: true })).toBe(`<span class="bulkbar" aria-busy="true">${ACTS}<span class="bb-scope">listing the matches…</span></span>`)
  })
  it('over the cap (gcs 10-09 `nemotron`): the count, the actions still enabled (the click says why, muted)', () => {
    const c = cover({ complete: false, items: [], roots: { n: 70_450, b: 718, o: 6_580_000 }, reason: overCapReason(70_390) })
    expect(render(c)).toBe(`<span class="bulkbar">${ACTS}<span class="bb-scope">70,450 matches · 718 B</span></span>`)
  })
  it('only the actions the viewer may take; none: nothing', () => {
    expect([text(render(undefined, { canStage: false })), text(render(undefined, { canAssign: false })), render(undefined, { canAssign: false, canStage: false })]).toEqual([
      ['assign to me'], ['stage for deletion'], '',
    ])
  })
})

describe('deselect: a folder\'s worth or one item, and what each action then sends', () => {
  const c = cover()
  it('groups by parent folder, heaviest first', () => {
    expect(groupItems(c.items).map(g => [g.parent, g.items.map(i => i.path.split('/').pop()), g.b, g.o])).toEqual([
      [SHOW, ['ACS_Rotten_Tomatoes_chunk000.mp3', 'ACS_Rotten_Tomatoes_chunk001.mp3', 'ACS_Rotten_Tomatoes_chunk002.mp3'], 300, 3],
      ['marin-eu-west4', ['tomat'], 50, 2],
    ])
  })
  it('dropping a folder\'s group leaves the rest; dropping one item leaves its siblings; ticking restores', () => {
    const kept = (d: ReadonlySet<string>) => c.items.filter(i => !d.has(i.path)).map(i => i.path)
    const noShow = applyToggle(new Set(), groupItems(c.items)[0].items.map(i => i.path), true)
    expect(kept(noShow)).toEqual(['marin-eu-west4/tomat'])
    const noOne = applyToggle(new Set(), [clips[1].path], true)
    expect(kept(noOne)).toEqual(['marin-eu-west4/tomat', clips[0].path, clips[2].path])
    expect(kept(applyToggle(noOne, [clips[1].path], false))).toEqual(c.items.map(i => i.path))
  })
  it('a folder goes as its prefix, a lone file as that exact object (never `key/`); stage never a bucket', () => {
    const items = [...c.items, { path: 'tomatoes-bucket', kind: 'dir' as const, b: 9, o: 1, roots: 1 }, { path: 'x/unk', kind: null, b: 1, o: 1, roots: 1 }]
    const files = clips.map(i => ({ key: `gs://${i.path}`, kind: 'object' }))
    expect(actionTargets(items, 'gs://', 'assign')).toEqual({
      items: [{ key: 'gs://marin-eu-west4/tomat/', kind: 'prefix' }, ...files, { key: 'gs://tomatoes-bucket/', kind: 'prefix' }],
      b: 359, o: 6, folders: 2, files: 3, buckets: 0, unknown: 1,
    })
    expect(actionTargets(items, 'gs://', 'stage')).toEqual({
      items: [{ key: 'gs://marin-eu-west4/tomat/', kind: 'prefix' }, ...files],
      b: 350, o: 5, folders: 1, files: 3, buckets: 1, unknown: 1,
    })
    expect(targetsText(actionTargets(items, 'gs://', 'stage'))).toBe('1 folder, 3 files')
    expect(targetsText({ folders: 0, files: 1 })).toBe('1 file')
  })
  it('says what it leaves out, in plain words (lone files are no longer left out)', () => {
    expect(caveats({ buckets: 0, unknown: 1 }, 'assign')).toEqual([
      '1 match couldn’t be checked (file or folder?) and is left out; open its folder to act on it.',
    ])
    expect(caveats({ buckets: 2, unknown: 0 }, 'stage')).toEqual([
      '2 whole buckets can’t be staged; they are left out.',
    ])
  })
})

// A rollup, catalog, refused or approximate view (`actBlock`): no actions, the reason muted in a tooltip.
describe('blocked: the view lists no matches to act on', () => {
  it('a muted label in the actions\' place, its reason in the tooltip; nothing listed even with a cover; null blocks nothing', () => {
    const why = actBlock({ bucketsOnly: true })!
    expect([render(cover(), { blocked: why }), render(undefined, { blocked: why, canStage: false }), render(undefined, { blocked: why, canAssign: false, canStage: false }), text(render(undefined, { blocked: null }))]).toEqual([
      `<span class="bulkbar blocked"><span class="tt"><span class="act-st muted" tabindex="0">${ACT_BLOCKED_LABEL}</span><span class="tt-tip"><span class="bb-tip">${why}</span></span></span></span>`,
      `<span class="bulkbar blocked"><span class="tt"><span class="act-st muted" tabindex="0">${ACT_BLOCKED_LABEL}</span><span class="tt-tip"><span class="bb-tip">${why}</span></span></span></span>`,
      '',
      ['assign to me', 'stage for deletion'],
    ])
  })
  it('no matches: no assign, stage or memo — the muted "no matches to act on", the reason in the tooltip', () => {
    const why = actBlock({ none: true })!
    expect([render(undefined, { blocked: why }), render(undefined, { blocked: why, canStage: false }), render(undefined, { blocked: why, canAssign: false })]).toEqual([
      `<span class="bulkbar blocked"><span class="tt"><span class="act-st muted" tabindex="0">no matches to act on</span><span class="tt-tip"><span class="bb-tip">${why}</span></span></span></span>`,
      `<span class="bulkbar blocked"><span class="tt"><span class="act-st muted" tabindex="0">no matches to act on</span><span class="tt-tip"><span class="bb-tip">${why}</span></span></span></span>`,
      `<span class="bulkbar blocked"><span class="tt"><span class="act-st muted" tabindex="0">no matches to act on</span><span class="tt-tip"><span class="bb-tip">${why}</span></span></span></span>`,
    ])
  })
})
