import { describe, expect, it } from 'vitest'
import { signInNext } from './signInNext'

const next = (raw: string) => signInNext(`?next=${encodeURIComponent(raw)}`)

describe('where `/signin` returns to', () => {
  it.each([
    ['a backslash, which a browser reads as a slash', '/\\example.test'],
    ['a tab, which a browser drops', '/\t/example.test'],
    ['a newline, which a browser drops', '/\n/example.test'],
    ['dot segments that resolve to `//`', '/x/..//example.test'],
    ['two slashes', '//example.test'],
    ['an absolute URL', 'https://example.test/x'],
    ['a scheme a browser would run', 'javascript:alert(1)'],
    ['a leading backslash', '\\\\example.test'],
    ['nothing', ''],
  ])('%s goes home', (_, raw) => {
    expect(next(raw)).toBe('/')
  })

  it('no `next` goes home', () => {
    expect(signInNext('')).toBe('/')
  })

  it.each([
    ['/', '/'],
    ['/scans', '/scans'],
    ['/scans?d=2026-11-01&p=a%2Fb', '/scans?d=2026-11-01&p=a%2Fb'],
    ['/a/b?x=1#frag', '/a/b?x=1#frag'],
    ['/a//b', '/a//b'],
    ['/%5Cexample.test', '/%5Cexample.test'],
  ])('a path on this origin keeps its query: %s', (raw, want) => {
    expect(next(raw)).toBe(want)
  })

  it('canonical: dot segments that stay on this origin resolve', () => {
    expect(next('/a/../scans?d=1')).toBe('/scans?d=1')
  })
})
