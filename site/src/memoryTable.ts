import type { TableColumn } from '@rdub/file-tree/renderers/table'
import type { TableCatalog } from '@rdub/file-tree/renderers/tableSource'

/** Small, already-loaded results use the same table UI as remote artifacts. */
export function memoryTable(columns: TableColumn[], rows: Record<string, unknown>[], name = 'rows'): TableCatalog {
  return {
    objects: async () => [{ name, type: 'table' }],
    source: () => ({
      capabilities: { sort: true, filter: true, total: true, randomAccess: true },
      columns: async () => columns,
      page: async ({ offset, limit, filter, sort }) => {
        const terms = (filter ?? '').toLowerCase().split(/\s+/).filter(Boolean)
        const filtered = rows.filter(row => terms.every(term => Object.values(row).some(v => String(v ?? '').toLowerCase().includes(term))))
        if (sort) filtered.sort((a, b) => {
          const av = a[sort.column], bv = b[sort.column]
          const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av ?? '').localeCompare(String(bv ?? ''))
          return (sort.dir === 'asc' ? cmp : -cmp) || String(a.prefix ?? a.name).localeCompare(String(b.prefix ?? b.name))
        })
        return { rows: filtered.slice(offset, offset + limit), columns, offset, total: filtered.length }
      },
    }),
  }
}
