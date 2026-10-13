import { describe, expect, it } from 'vitest'
import { isUserScroll, navScroll, resetUserScroll } from './hashSpy'
import type { NavLocation } from './hashSpy'

const loc = (pathname: string, hash = '', key = 'k0'): NavLocation => ({ pathname, hash, key })

describe('navScroll', () => {
  it('breadcrumb root from a scrolled, hashed bucket → the top, no section', () => {
    // The spy had written #tbl (the router never saw it); the crumb pushes `/`.
    expect(navScroll(loc('/marin-us-central2', '', 'a'), loc('/', '', 'b'), 'PUSH', '#tbl', undefined))
      .toEqual({ kind: 'top' })
  })
  it('a stale router hash is not carried: the crumb to `/` stays at the top even if the old entry was linked to #over-time', () => {
    expect(navScroll(loc('/marin-us-central2', '#over-time', 'a'), loc('/', '', 'b'), 'PUSH', '#tbl', undefined))
      .toEqual({ kind: 'top' })
  })
  it('a drill → the top', () => {
    expect(navScroll(loc('/b', '', 'a'), loc('/b/dir', '', 'b'), 'PUSH', '#diff', undefined))
      .toEqual({ kind: 'top' })
  })
  it('a link that names a section on another path scrolls to it', () => {
    expect(navScroll(loc('/b', '', 'a'), loc('/b/dir', '#over-time', 'b'), 'PUSH', '', undefined))
      .toEqual({ kind: 'hash', id: 'over-time' })
  })
  it('back / forward restores the entry’s position, over its hash', () => {
    expect(navScroll(loc('/', '', 'b'), loc('/marin-us-central2', '#tbl', 'a'), 'POP', '', 1840))
      .toEqual({ kind: 'restore', y: 1840 })
  })
  it('back to an entry with no saved position → its hash, else the top', () => {
    expect(navScroll(loc('/', '', 'b'), loc('/b', '#diff', 'a'), 'POP', '', undefined))
      .toEqual({ kind: 'hash', id: 'diff' })
    expect(navScroll(loc('/', '', 'b'), loc('/b', '', 'a'), 'POP', '', undefined))
      .toEqual({ kind: 'top' })
  })
  it('a deep link scrolls to its section; a reload restores the position', () => {
    expect(navScroll(null, loc('/b', '#tbl', 'default'), 'POP', '#tbl', undefined))
      .toEqual({ kind: 'hash', id: 'tbl' })
    expect(navScroll(null, loc('/b', '#tbl', 'a'), 'POP', '#tbl', 920))
      .toEqual({ kind: 'restore', y: 920 })
    expect(navScroll(null, loc('/b', '', 'default'), 'POP', '', undefined))
      .toEqual({ kind: 'none' })
  })
  it('same-path param changes (`?d=`, use-prms re-syncing the router to the spy’s hash) never move the page', () => {
    // use-prms pushes with the entry's state copied (same key) and fires a synthetic popstate.
    expect(navScroll(loc('/b', '#over-time', 'a'), loc('/b', '#tbl', 'a'), 'POP', '#tbl', 300))
      .toEqual({ kind: 'none' })
    // A legacy-param rewrite: REPLACE, a new key, the live hash kept.
    expect(navScroll(loc('/b', '', 'a'), loc('/b', '#diff', 'c'), 'REPLACE', '#diff', undefined))
      .toEqual({ kind: 'none' })
    // Opening an object: same path, no hash.
    expect(navScroll(loc('/b', '', 'a'), loc('/b', '', 'c'), 'PUSH', '#tbl', undefined))
      .toEqual({ kind: 'none' })
  })
  it('a same-path link to a section other than the live one scrolls to it', () => {
    expect(navScroll(loc('/b', '', 'a'), loc('/b', '#mtime', 'c'), 'PUSH', '#tbl', undefined))
      .toEqual({ kind: 'hash', id: 'mtime' })
  })
})

describe('isUserScroll', () => {
  it('a scroll with no reader input (programmatic, a layout clamp) is not the reader’s', () => {
    resetUserScroll()
    // Even in the page's first second (`performance.now()` near 0).
    expect([isUserScroll(5), isUserScroll(performance.now())]).toEqual([false, false])
  })
})
