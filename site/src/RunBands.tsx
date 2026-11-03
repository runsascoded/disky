import { useMemo } from 'react'
import { TableBrowser } from '@rdub/file-tree/renderers/tableBrowser'
import { memoryTable } from './memoryTable'
import { CopyName, elideMid } from './CopyName'
import { Tooltip } from './Tooltip'
import { fmtN } from './types'
import type { RunAnalytics, RunBand } from './plans'
import { SkippedCounts } from './SkippedCounts'

export function RunBands({ bands, id, fmtBytes, analysis }: { bands: RunBand[]; id: string; fmtBytes: (b: number) => string; analysis?: RunAnalytics['analysis'] }) {
  const catalog = useMemo(() => memoryTable([
    { name: 'prefix', kind: 'string' },
    ...['bytes', 'objects', 'gone', 'skipped', 'undone'].map(name => ({ name, kind: 'number' as const })),
  ], bands.map(b => ({ prefix: b.prefix, bytes: b.bytes, objects: b.objects, gone: b.gone, skipped: b.overwritten + b.drift_new_objects, undone: b.undone_objects }))), [bands])
  const byPrefix = useMemo(() => new Map(bands.map(b => [b.prefix, b])), [bands])
  return <div className="run-band-table">
    <TableBrowser catalog={catalog} objects={[{ name: 'rows', type: 'table' }]} path={`run-bands/${id}`} pageSize={25}
      resizableColumns={{ scope: 'path' }}
      elide={{ tooltip: ({ node, text }) => <Tooltip content={text}>{node}</Tooltip> }}
      renderCell={({ column, value, row }) => column.name === 'prefix'
        ? <CopyName text={String(value)}><code>{elideMid(String(value), 100, 24)}</code></CopyName>
        : column.name === 'skipped' ? <SkippedCounts band={byPrefix.get(String(row.prefix))!} analysis={analysis} />
          : column.name === 'bytes' ? fmtBytes(Number(value)) : fmtN(Number(value))}
    />
  </div>
}
