import { TitleSpaceToggle, Treemap } from '@rdub/treemap'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { FederatedScanPicker } from '../FederatedScanPicker'
import { federatedDeltas, parseSourceCatalog, resolveFederation, utcInstant, type SourceCatalog } from '../federatedScans'
import { composeFederatedTrees, federatedSeries } from '../federatedViews'
import { useUnits } from '../units'
import type { TreeNode } from '../types'
import '@rdub/treemap/styles.css'

const t = (day: number, hour: number) => `2026-10-${day}T${String(hour).padStart(2, '0')}:00:00.000Z`
/** Deliberately fictional, completed exports. Never presented as production
 * inventory, and this route is not included in production builds. */
const catalogs: SourceCatalog[] = ['ctbk', 'crashes', 'jc-taxes', 'awair'].map((source, i) => parseSourceCatalog({
  version: 1, source, label: source, mount: source, scope: { store: `r2:fixture/${source}`, prefix: '' },
  scans: [12, 13].map(day => ({
    id: `${source}-${day}`, revision: 'fixture-1', digest: String(i + day - 10).repeat(64), complete: true,
    observed_from: t(day, i * 2 + 1), observed_through: t(day, i * 2 + 2), published_at: t(day, i * 2 + 3),
    total_bytes: Math.floor((i === 0 ? 880 : i === 1 ? 80 : i === 2 ? 9 : .1) * 1024 ** 3) + (day - 12) * (i + 1) * 1024 ** 2,
    total_objects: 1000 * (i + 1) + day,
    artifacts: { format: 'path-store-v2', path: `scans/${source}/${day}/path.parquet` },
  })),
}))
const defaults = { at: t(13, 9), before: t(12, 9), after: t(13, 9) }
const now = () => t(13, 9)

function cutoff(value: string | null, fallback: string): string {
  if (value === null) return fallback
  return utcInstant(value)
}

export default function FederatedScansPreview() {
  const [reclaimTitleSpace, setReclaimTitleSpace] = useState(false)
  const [renderer, setRenderer] = useState<'dom' | 'canvas'>('dom')
  const [params, setParams] = useSearchParams()
  const { fmtBytes } = useUnits()
  let values: typeof defaults
  try { values = { at: cutoff(params.get('at'), defaults.at), before: cutoff(params.get('before'), defaults.before), after: cutoff(params.get('after'), defaults.after) } }
  catch { return <main className="federated-preview"><h1>Independent scans, one view</h1><p role="alert">Invalid cutoff URL. Use valid UTC instants for at, before and after.</p><a href="/dev/federated-scans">Reset development fixture</a></main> }
  const view = resolveFederation(catalogs, values.at)
  const { tree, missing } = composeFederatedTrees(view, view.sources.flatMap(s => s.scan ? [{ source: s.source, digest: s.scan.digest,
    tree: { n: s.source, k: 'dir' as const, b: s.scan.total_bytes, o: s.scan.total_objects, c: [
      { n: 'archive', k: 'dir' as const, b: Math.floor(s.scan.total_bytes * .7), o: s.scan.total_objects - 2 },
      { n: 'recent', k: 'dir' as const, b: s.scan.total_bytes - Math.floor(s.scan.total_bytes * .7), o: 2 },
    ] },
  }] : []))
  const deltas = federatedDeltas(resolveFederation(catalogs, values.before), resolveFederation(catalogs, values.after))
  const series = federatedSeries(catalogs)
  const points = [...new Set(series.flatMap(s => s.points.map(p => p.at)))].map(at => ({ at,
    sources: series.map(s => ({ source: s.source, point: s.points.find(p => p.at === at)! })),
  }))
  return <main className="federated-preview">
    <h1>Independent scans, one view</h1>
    <p>Development fixture — fictional sizes and schedules, not a live R2 inventory. Each project keeps its own scans; the cutoff selects its latest completed observation.</p>
    <div className="fp-main-controls">{(['at', 'before', 'after'] as const).map(key => <FederatedScanPicker key={key} catalogs={catalogs} value={values[key]} now={now}
      label={key === 'at' ? 'As of' : key === 'before' ? 'Compare from' : 'Compare to'} onChange={at => setParams(p => { const next = new URLSearchParams(p); next.set(key, at); return next })} />)}</div>
    <div className="fp-preview-totals">{fmtBytes(tree.b)} · {tree.o.toLocaleString()} objects{missing.length > 0 && <span role="status"> · Partial coverage: no earlier scan for {missing.join(', ')}</span>}</div>
    <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBlock: 12 }}>
      <TitleSpaceToggle checked={reclaimTitleSpace} onChange={setReclaimTitleSpace} />
      <label>Renderer <select value={renderer} onChange={e => setRenderer(e.target.value as 'dom' | 'canvas')}><option value="dom">DOM</option><option value="canvas">Canvas</option></select></label>
    </div>
    <div style={{ height: 320 }}><Treemap<TreeNode> root={tree} getChildren={n => n.c} getLabel={n => n.n} getSize={n => n.b} formatSize={fmtBytes} collapseChains renderer={renderer} reclaimTitleSpace={reclaimTitleSpace} branchOutline="color-mix(in oklab, var(--ink) 30%, transparent)" /></div>
    <h2>Diff</h2>
    {values.before > values.after && <p role="status">Reverse comparison: changes are “to” minus “from”.</p>}
    <table className="fp-preview-table"><thead><tr><th>Project</th><th>Bytes change</th><th>Objects change</th></tr></thead>
      <tbody>{deltas.map(d => <tr key={d.source}><th>{d.source}</th><td>{d.bytes === null ? 'Unknown coverage' : fmtBytes(d.bytes)}</td><td>{d.objects === null ? 'Unknown coverage' : d.objects.toLocaleString()}</td></tr>)}</tbody></table>
    <details><summary>Over-time event values (step-series adapter)</summary><table className="fp-preview-table"><thead><tr><th>Observed update (UTC)</th>{series.map(s => <th key={s.source}>{s.source}</th>)}</tr></thead>
      <tbody>{points.map(p => <tr key={p.at}><th>{p.at}</th>{p.sources.map(s => <td key={s.source}>{s.point.bytes === null ? 'Unknown' : fmtBytes(s.point.bytes)}</td>)}</tr>)}</tbody></table></details>
  </main>
}
