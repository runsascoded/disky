import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { CostSnapshot } from './costModel'
import { bucketSoftDeleteOf, minimumStatus, SweepSoftDeleteView, type SweepSoftDeleteViewProps } from './SweepSoftDelete'

const nop = () => {}
const DEFAULT = { settings: null, min_soft_delete_days: 7 }
const view = (over: Partial<SweepSoftDeleteViewProps>) => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(SweepSoftDeleteView, {
  answer: DEFAULT, draft: '7', onDraft: nop, understood: false, onUnderstood: nop, onSave: nop, buckets: null, busy: false, error: null, ...over,
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
  it('lists what each bucket has, by name, when a snapshot says', () => {
    const html = view({ buckets: { asOf: '2026-11-04', off: { 'b-on': false, 'a-off': true } } })
    expect([...html.matchAll(/<tr><td><code>([^<]+)<\/code><\/td><td( class="warn")?>(on|off)<\/td><\/tr>/g)].map(m => [m[1], m[3], !!m[2]])).toEqual([['a-off', 'off', true], ['b-on', 'on', false]])
    expect(/<table/.test(view({ buckets: null }))).toBe(false)
  })
})

describe('bucketSoftDeleteOf', () => {
  it('reads the newest document, null without one', () => {
    const doc = (date: string, off: boolean) => ({ date, storage: { buckets: [{ bucket: 'b', soft_delete_off: off }] } }) as unknown as CostSnapshot
    expect(bucketSoftDeleteOf([doc('2026-11-03', false), doc('2026-11-04', true)])).toEqual({ asOf: '2026-11-04', off: { b: true } })
    expect(bucketSoftDeleteOf([])).toBeNull()
    expect(bucketSoftDeleteOf([{ date: 'd', storage: null } as unknown as CostSnapshot])).toBeNull()
  })
})
