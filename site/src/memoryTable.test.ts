import { expect, it } from 'vitest'
import { memoryTable } from './memoryTable'

it('uses the shared pager to filter, sort numerically, and count matching rows', async () => {
  const columns = [{ name: 'prefix', kind: 'string' as const }, { name: 'bytes', kind: 'number' as const }]
  const rows = [{ prefix: 'gs://b/sft/z/', bytes: 2 }, { prefix: 'gs://b/ego/', bytes: 50 }, { prefix: 'gs://b/sft/a/', bytes: 100 }]
  const source = memoryTable(columns, rows).source('rows')
  expect(source.capabilities).toEqual({ sort: true, filter: true, total: true, randomAccess: true })
  expect(await source.page({ offset: 0, limit: 1, filter: 'b sft', sort: { column: 'bytes', dir: 'desc' } })).toEqual({ columns, rows: [rows[2]], offset: 0, total: 2 })
  expect(await source.page({ offset: 1, limit: 1, filter: 'b sft', sort: { column: 'bytes', dir: 'desc' } })).toEqual({ columns, rows: [rows[0]], offset: 1, total: 2 })
  expect(await source.page({ offset: 0, limit: 25, filter: 'missing' })).toEqual({ columns, rows: [], offset: 0, total: 0 })
  expect(rows).toEqual([{ prefix: 'gs://b/sft/z/', bytes: 2 }, { prefix: 'gs://b/ego/', bytes: 50 }, { prefix: 'gs://b/sft/a/', bytes: 100 }])
})
