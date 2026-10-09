import type { TableCatalog, TableSource } from '@rdub/file-tree/renderers/tableSource'
import type { TableColumn } from '@rdub/file-tree/renderers/table'

export interface ArtifactEntry { key: string; isDir?: boolean; size?: number }
export interface ArtifactList { entries: ArtifactEntry[]; cursor?: string }
const COLUMNS: TableColumn[] = [{ name: 'name', kind: 'string' }, { name: 'kind', kind: 'string' }, { name: 'bytes', kind: 'number' }, { name: 'download', kind: 'string' }]

/** Cursor listing adapted to file-tree's existing forward-only table pager. */
export function artifactCatalog(folder: string, root: string, list: (cursor: string | undefined, limit: number) => Promise<ArtifactList>): TableCatalog {
  if (!folder.startsWith(root)) throw new Error('Artifact folder outside run')
  const offsets = new Map<number, string | undefined>([[0, undefined]])
  let pageSize: number | null = null
  let total: number | null = null
  const source: TableSource = {
    capabilities: { sort: false, filter: false, total: false, randomAccess: false },
    columns: async () => COLUMNS,
    page: async ({ offset, limit }) => {
      if (offset < 0 || !Number.isInteger(offset) || limit < 1 || offset % limit) throw new Error('Invalid artifact page')
      if (pageSize !== limit) { offsets.clear(); offsets.set(0, undefined); total = null; pageSize = limit }
      if (total !== null && offset >= total) return { rows: [], columns: COLUMNS, offset, total }
      // A cursor source can revisit an earlier page without relisting it all.
      let at = Math.max(...[...offsets.keys()].filter(n => n <= offset))
      while (true) {
        const result = await list(offsets.get(at), limit)
        if (result.entries.some(e => !e.key.startsWith(folder) || !e.key.startsWith(root))) throw new Error('Artifact listing escaped run folder')
        const nextAt = at + result.entries.length
        if (result.cursor) {
          if (!result.entries.length || result.cursor === offsets.get(at)) throw new Error('Artifact cursor did not advance')
          offsets.set(nextAt, result.cursor)
        } else total = nextAt
        if (at === offset) return { rows: result.entries.map(e => ({ name: e.key.slice(folder.length), key: e.key, kind: e.isDir ? 'folder' : 'file', bytes: e.size ?? null, download: e.isDir ? null : e.key })), columns: COLUMNS, offset, total }
        if (nextAt > offset) throw new Error('Artifact listing returned an unexpected page size')
        if (!result.cursor) return { rows: [], columns: COLUMNS, offset, total }
        at = nextAt
      }
    },
  }
  return { objects: async () => [{ name: 'files', type: 'table' }], source: () => source }
}
