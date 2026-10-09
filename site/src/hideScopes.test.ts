import { describe, expect, it } from 'vitest'
import { hideScopes } from './hideScopes'

describe('hideScopes', () => {
  const home = ['Users', 'ryan']
  it('each line → one scope: `~`, absolute and store-relative paths; a secondary store by its key; blanks and dupes dropped', () => {
    expect(hideScopes('~/Downloads/2025 tax returns\n\n/Users/ryan/Downloads/2025 tax returns/\nnas: mnt/user/backups\nUsers/ryan/a,b\n', 'laptop', ['nas'], home)).toEqual([
      'laptop:hide:Users/ryan/Downloads/2025%20tax%20returns',
      'nas:hide:mnt/user/backups',
      'laptop:hide:Users/ryan/a%2Cb',
    ])
  })
  it('an unknown `x:` lead is part of the path, on the primary', () => {
    expect(hideScopes('c:/stuff', 'laptop', ['nas'], home)).toEqual(['laptop:hide:c:/stuff'])
  })
  it('encodes as the server’s `hideScope` (`restrict.test.ts` pins the same strings)', () => {
    expect(hideScopes('/x, y/100%/', 'nas', [], home)).toEqual(['nas:hide:x%2C%20y/100%25'])
  })
})
