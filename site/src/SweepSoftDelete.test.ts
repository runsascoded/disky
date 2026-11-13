import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { bucketState, minimumStatus, SweepSoftDeleteView, type SweepSoftDeleteViewProps } from './SweepSoftDelete'

const nop = () => {}
const DEFAULT = { settings: null, min_soft_delete_days: 7 }
const view = (over: Partial<SweepSoftDeleteViewProps>) => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(SweepSoftDeleteView, {
  answer: DEFAULT, draft: '7', onDraft: nop, understood: false, onUnderstood: nop, onSave: nop, buckets: null, swept: [], busy: false, error: null, ...over,
})))
/** The `<p class="warn">`'s text, or null. */
const warning = (html: string) => /<p class="warn" role="alert">([^<]*)<\/p>/.exec(html)?.[1]?.replace(/&#x27;/g, "'") ?? null
const saveDisabled = (html: string) => /<button type="submit"( disabled="")?>Save/.exec(html)![1] !== undefined

describe('minimumStatus', () => {
  it('states the default, or who set it and when', () => {
    expect(minimumStatus(DEFAULT)).toBe('Minimum: 7 days (the default; never set here).')
    expect(minimumStatus({ settings: { min_soft_delete_days: 0, updated_by: 'ops', updated_at: 1_790_000_000 }, min_soft_delete_days: 0 })).toBe('Minimum: 0 days. Set by ops on 2026-09-21.')
    expect(minimumStatus({ settings: { min_soft_delete_days: 1, updated_by: 'ops', updated_at: 1_790_000_000 }, min_soft_delete_days: 1 })).toBe('Minimum: 1 day. Set by ops on 2026-09-21.')
  })
})

describe('the editor', () => {
  it('says plainly what going below 7 does, and asks before saving', () => {
    const zero = view({ draft: '0' })
    expect(warning(zero)).toBe('At 0 there is no undo: a run deletes from a bucket with soft delete off, and those deletes are permanent. Nothing can restore them.')
    expect(saveDisabled(zero)).toBe(true)
    expect(saveDisabled(view({ draft: '0', understood: true }))).toBe(false)
    expect(warning(view({ draft: '3' }))).toBe('Below 7 days the undo window is shorter: a run accepts a bucket retaining soft deletes for as little as 3 days, and what it deletes can\'t be restored after that.')
  })
  it('has no warning at 7 or above, and nothing to save until the value changes', () => {
    const same = view({ draft: '7' })
    expect([warning(same), saveDisabled(same)]).toEqual([null, true])
    const up = view({ draft: '14' })
    expect([warning(up), saveDisabled(up)]).toEqual([null, false])
  })
  it.each(['', '-1', '91', '1.5'])('refuses to save %j', draft => expect(saveDisabled(view({ draft }))).toBe(true))
  const rows = (html: string) => [...html.matchAll(/<tr><td><code>([^<]+)<\/code><\/td>(?:<td class="dim">([^<]*)<\/td>)?<td(?: class="(warn|dim)")?>([^<]+)<\/td><\/tr>/g)]
    .map(m => m[2] === undefined ? [m[1], m[4], m[3] ?? null] : [m[1], m[2], m[4], m[3] ?? null])
  const note = (html: string) => /<p class="dim">([^<]*)<\/p>/.exec(html)?.[1]?.replace(/&#x27;/g, "'") ?? null
  it('lists the buckets a sweep acts on, by name: what the reading says of each, "not read" where it says nothing', () => {
    const html = view({ swept: ['c-new', 'b-on', 'a-off'], buckets: { asOf: '2026-11-05 07:12Z', off: { 'b-on': false, 'a-off': true, 'not-swept': true } } })
    expect(rows(html)).toEqual([['a-off', 'off', 'warn'], ['b-on', 'on', null], ['c-new', 'not read', 'dim']])
    expect(note(html)).toBe('What each bucket a sweep acts on has, as the job read it at 2026-11-05 07:12Z (its soft-delete step, after a scan); a run measures its buckets itself when it starts:')
  })
  it('no reading: every bucket "not read", and says what that means', () => {
    const html = view({ swept: ['b', 'a'], buckets: null })
    expect(rows(html)).toEqual([['a', 'not read', 'dim'], ['b', 'not read', 'dim']])
    expect(note(html)).toBe("No reading of the buckets' soft delete (the job's soft-delete step writes one after a scan), so this site can't say what a past run's undo has to work with; a run measures its buckets itself when it starts:")
  })
  it('a "what it is" column when any swept bucket has a note, blank for one without', () => {
    const html = view({ swept: ['b', 'a'], buckets: null, notes: { a: 'raw training data', other: 'not swept' } })
    expect(/<th>what it is<\/th>/.test(html)).toBe(true)
    expect(rows(html)).toEqual([['a', 'raw training data', 'not read', 'dim'], ['b', '', 'not read', 'dim']])
    expect(/<th>what it is<\/th>/.test(view({ swept: ['b'], buckets: null, notes: { other: 'not swept' } }))).toBe(false)
  })
  it('no table for a store with no buckets', () => {
    expect(/<table/.test(view({ swept: [], buckets: { asOf: 'x', off: { a: true } } }))).toBe(false)
  })
  it('bucketState', () => {
    const known = { asOf: 'x', off: { on: false, off: true } }
    expect([bucketState('on', known), bucketState('off', known), bucketState('other', known), bucketState('toString', known), bucketState('on', null), bucketState('on', undefined)])
      .toEqual(['on', 'off', 'unknown', 'unknown', 'unknown', 'unknown'])
  })
})
