import { useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { stringParam, useUrlState } from 'use-prms'
import { TableBrowser } from '@rdub/file-tree/renderers/tableBrowser'
import { artifactCatalog, type ArtifactList } from './artifactCatalog'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'
import { runDirHref, runFileHref } from './runs'
import { useRunFiles } from './plans'
import { memoryTable } from './memoryTable'

export function RunPlan({ dir }: { dir: string }) {
  const [q] = useRunFiles<{ sweep?: string[]; approved?: string[] }>([`${dir}plan.json`], false)
  const prefixes = q.data?.sweep ?? q.data?.approved ?? []
  return <details className="run-plan"><summary>{prefixes.length} reviewed prefixes</summary>
    {q.error ? <p className="err">{q.error.message}</p> : prefixes.map(p => <div key={p}><code>{p}</code></div>)}
  </details>
}

/** Human-facing artifact navigation, confined to this run's log directory. */
export function RunArtifacts({ dir, buckets = [] }: { dir: string; buckets?: string[] }) {
  const [relative, setRelative] = useUrlState('af', stringParam(''))
  const folder = relative && !relative.includes('..') ? `${dir}${relative}` : dir
  const open = (key: string) => setRelative(key.slice(dir.length))
  const client = useQueryClient()
  const [revision, setRevision] = useState(0)
  const { fmtBytes } = useUnits()
  const catalog = useMemo(() => folder === dir && buckets.length ? memoryTable([
    { name: 'name', kind: 'string' }, { name: 'kind', kind: 'string' }, { name: 'bytes', kind: 'number' }, { name: 'download', kind: 'string' },
  ], buckets.map(b => ({ name: `${b}/`, key: `${dir}${b}/`, kind: 'folder', bytes: null, download: null })), 'files') : artifactCatalog(folder, dir, (cursor, limit) => client.fetchQuery<ArtifactList>({
    queryKey: ['run-artifacts', folder, cursor, limit, revision], staleTime: 30_000,
    queryFn: async () => {
      const r = await fetch(`${runDirHref(folder)}&limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`Decision files: HTTP ${r.status}`)
      return r.json()
    },
  })), [folder, dir, client, revision, buckets.join('|')])
  return <div className="run-artifacts">
    <p className="artifact-path"><code>{folder.slice(dir.length) || 'bucket logs'}</code>{folder !== dir && <> · <button type="button" onClick={() => open(dir)}>all buckets</button></>} · <button type="button" onClick={() => setRevision(n => n + 1)}>refresh files</button></p>
    <TableBrowser key={`${folder}-${revision}`} catalog={catalog} objects={[{ name: 'files', type: 'table' }]} path={folder} pageSize={25}
      resizableColumns={{ scope: 'path' }} headerProps={() => ({ style: { fontWeight: 650 } })}
      elide={{ tooltip: ({ node, text }) => <Tooltip content={text}>{node}</Tooltip> }}
      renderCell={({ column, row, value, defaultNode }) => column.name === 'name'
        ? row.kind === 'folder' ? <button type="button" onClick={() => open(String(row.key))}>{String(value)}</button> : <code>{String(value)}</code>
        : column.name === 'download' ? value ? <a href={runFileHref(String(value))} target="_blank" rel="noreferrer">download ↗</a> : '—'
        : column.name === 'bytes' ? value == null ? '—' : fmtBytes(Number(value)) : defaultNode}
    />
    <p className="dim">All planned buckets are listed, including those not started. An empty folder has no committed decision parts yet.</p>
  </div>
}
