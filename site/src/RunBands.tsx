import { useMemo } from 'react'
import { TableBrowser } from '@rdub/file-tree/renderers/tableBrowser'
import { memoryTable } from './memoryTable'
import { CopyName, elideMid } from './CopyName'
import { Tooltip } from './Tooltip'
import { fmtN } from './types'
import type { RunBand } from './plans'

export function RunBands({ bands, id, fmtBytes }: { bands: RunBand[]; id: string; fmtBytes: (b: number) => string }) {
  const catalog = useMemo(() => memoryTable([
    { name: 'prefix', kind: 'string' },
    ...['bytes', 'objects', 'gone', 'overwritten', 'drift new', 'undone'].map(name => ({ name, kind: 'number' as const })),
  ], bands.map(({ drift_new_objects, undone_objects, ...b }) => ({ ...b, 'drift new': drift_new_objects, undone: undone_objects }))), [bands])
  return <div className="run-band-table">
    <TableBrowser catalog={catalog} objects={[{ name: 'rows', type: 'table' }]} path={`run-bands/${id}`} pageSize={25}
      resizableColumns={{ scope: 'path' }}
      elide={{ tooltip: ({ node, text }) => <Tooltip content={text}>{node}</Tooltip> }}
      renderCell={({ column, value }) => column.name === 'prefix'
        ? <CopyName text={String(value)}><code>{elideMid(String(value), 100, 24)}</code></CopyName>
        : column.name === 'bytes' ? fmtBytes(Number(value)) : fmtN(Number(value))}
    />
  </div>
}
