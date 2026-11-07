import { expect, it } from 'vitest'
import { artifactCatalog } from './artifactCatalog'

it('uses real cursor boundaries and reports the exact total only on the last page', async () => {
  const calls: [string | undefined, number][] = []
  const catalog = artifactCatalog('r/b/', 'r/', async (cursor, limit) => {
    calls.push([cursor, limit])
    return cursor ? { entries: [{ key: 'r/b/c', size: 30 }] } : { entries: [{ key: 'r/b/a', size: 10 }, { key: 'r/b/b', isDir: true }], cursor: 'next' }
  })
  const source = catalog.source('files')
  expect(source.capabilities).toEqual({ sort: false, filter: false, total: false, randomAccess: false })
  const first = await source.page({ offset: 0, limit: 2 })
  const second = await source.page({ offset: 2, limit: 2 })
  expect([first.rows, first.total, second.rows, second.total, calls]).toEqual([
    [{ name: 'a', key: 'r/b/a', kind: 'file', bytes: 10, download: 'r/b/a' }, { name: 'b', key: 'r/b/b', kind: 'folder', bytes: null, download: null }], null,
    [{ name: 'c', key: 'r/b/c', kind: 'file', bytes: 30, download: 'r/b/c' }], 3, [[undefined, 2], ['next', 2]],
  ])
})

it('rejects artifacts outside the requested run rather than exposing them', async () => {
  const source = artifactCatalog('r/b/', 'r/', async () => ({ entries: [{ key: 'other/secret' }] })).source('files')
  await expect(source.page({ offset: 0, limit: 25 })).rejects.toThrow('Artifact listing escaped run folder')
})
