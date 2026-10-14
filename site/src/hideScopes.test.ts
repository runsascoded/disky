import { describe, expect, it } from 'vitest'
import { hideScopes } from './hideScopes'

describe('hideScopes', () => {
  const home = ['Users', 'ryan']
  it('each line → one scope: `~`, absolute and store-relative paths; a secondary store by its key; blanks and dupes dropped', () => {
    expect(hideScopes('~/Downloads/2025 tax returns\n\n/Users/ryan/Downloads/2025 tax returns/\nnas: mnt/user/backups\nUsers/ryan/a,b\n', 'laptop', ['nas'], home)).toEqual({
      scopes: [
        'laptop:hide:Users/ryan/Downloads/2025%20tax%20returns',
        'nas:hide:mnt/user/backups',
        'laptop:hide:Users/ryan/a%2Cb',
      ],
      unresolved: [],
    })
  })
  it('an unknown `x:` lead is part of the path, on the primary', () => {
    expect(hideScopes('c:/stuff', 'laptop', ['nas'], home)).toEqual({ scopes: ['laptop:hide:c:/stuff'], unresolved: [] })
  })
  it('encodes as the server’s `hideScope` (`restrict.test.ts` pins the same strings)', () => {
    expect(hideScopes('/x, y/100%/', 'nas', [], home)).toEqual({ scopes: ['nas:hide:x%2C%20y/100%25'], unresolved: [] })
  })
  it('`~` with no home dir (a bucket store), or on a secondary store, is unresolved: no scope, never the store root', () => {
    expect(hideScopes('~/Downloads\nbk/private\n~\nnas:~/x', 'gcs', ['nas'])).toEqual({
      scopes: ['gcs:hide:bk/private'],
      unresolved: ['~/Downloads', '~', 'nas:~/x'],
    })
  })
})
