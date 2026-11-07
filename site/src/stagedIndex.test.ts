import { describe, expect, it } from 'vitest'
import { stagedIndex } from './stagedIndex'
import type { StagedItem } from './plans'

const item = (prefix: string, kind?: 'prefix' | 'object'): StagedItem =>
  ({ prefix, kind, note: null, added_by: 'a@b.c', added_ts: 0, batch_id: 1, as_of: null })

describe('stagedIndex', () => {
  const idx = stagedIndex([
    item('file:///Users/ryan/Downloads/Photos-1-001 (2).zip', 'object'),
    item('file:///Users/ryan/Downloads/tmp/'),
    item('file:///Users/ryan/c/a/node_modules/', 'prefix'),
    item('file:///Users/ryan/c/b/.venv/'),
  ])
  const mark = (path: string, dir: boolean) => idx.markOf(`file:///${path}`, dir)

  it('marks a staged object and a staged folder themselves', () => {
    expect([mark('Users/ryan/Downloads/Photos-1-001 (2).zip', false), mark('Users/ryan/Downloads/tmp', true)])
      .toEqual([{ as: 'self' }, { as: 'self' }])
  })
  it('a folder item never marks a same-named object, nor an object item a folder', () => {
    expect([mark('Users/ryan/Downloads/tmp', false), mark('Users/ryan/Downloads/Photos-1-001 (2).zip', true)])
      .toEqual([null, null])
  })
  it('anything under a staged folder is covered by it', () => {
    expect([mark('Users/ryan/Downloads/tmp/x/y.bin', false), mark('Users/ryan/Downloads/tmp/x', true)]).toEqual([
      { as: 'covered', by: 'file:///Users/ryan/Downloads/tmp/' },
      { as: 'covered', by: 'file:///Users/ryan/Downloads/tmp/' },
    ])
  })
  it('a folder holding staged items counts them; a name prefix is not a parent', () => {
    expect([mark('Users/ryan/Downloads', true), mark('Users/ryan/c', true), mark('Users/ryan', true), mark('Users/ryan/Down', true), mark('Users/ryan/c/a', true)])
      .toEqual([{ as: 'inside', n: 2 }, { as: 'inside', n: 2 }, { as: 'inside', n: 4 }, null, { as: 'inside', n: 1 }])
  })
  it('nothing staged, nothing marked; bucket schemes work the same', () => {
    expect(stagedIndex([]).markOf('file:///Users', true)).toEqual(null)
    const gs = stagedIndex([item('gs://b/ckpt/'), item('gs://b/x.bin', 'object')])
    expect([gs.markOf('gs://b', true), gs.markOf('gs://b/ckpt/step1', true), gs.markOf('gs://b/x.bin', false), gs.markOf('gs://c', true)])
      .toEqual([{ as: 'inside', n: 2 }, { as: 'covered', by: 'gs://b/ckpt/' }, { as: 'self' }, null])
  })
  it('`isItem` is the plan items themselves only; `size` counts them', () => {
    expect([idx.size, mark('Users/ryan/Downloads/tmp', true) && idx.isItem('file:///Users/ryan/Downloads/tmp', true), idx.isItem('file:///Users/ryan/Downloads/tmp/x', true), idx.isItem('file:///Users/ryan/Downloads', true), idx.isItem('file:///Users/ryan/Downloads/Photos-1-001 (2).zip', false)])
      .toEqual([4, true, false, false, true])
  })
})
