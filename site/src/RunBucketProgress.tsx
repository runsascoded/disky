import { DEFAULT_PALETTE, Treemap } from '@rdub/treemap'
import { useMemo } from 'react'
import { stringParam, useUrlState } from 'use-prms'
import { type RunBand, useRunFiles } from './plans'
import { percentOf } from './runChart'
import { fmtN } from './types'
import { Tooltip } from './Tooltip'
import type { LogSummaryFile, PlanSummaryFile, ProgressFile } from './runs'

interface BucketNode { name: string; prefix?: string; bytes: number; objects: number; deletedBytes: number; deletedObjects: number; known: boolean; state: string; updated?: string | null; children?: BucketNode[] }

/** Final decision bands are exact per-prefix acknowledgments, not current scan sizes. */
export function deletionTree(bands: readonly RunBand[]): BucketNode {
  const root: BucketNode = { name: 'recorded deletions', bytes: 0, objects: 0, deletedBytes: 0, deletedObjects: 0, known: true, state: 'recorded', children: [] }
  for (const band of bands) {
    let node = root
    const parts = band.prefix.replace(/^gs:\/\//, '').split('/').filter(Boolean)
    for (let i = 0; i <= parts.length; i++) {
      node.bytes += band.bytes; node.deletedBytes += band.bytes
      node.objects += band.objects; node.deletedObjects += band.objects
      if (i === parts.length) break
      const prefix = `gs://${parts.slice(0, i + 1).join('/')}/`
      node.children ??= []
      let child = node.children.find(c => c.prefix === prefix)
      if (!child) {
        child = { name: parts[i], prefix, bytes: 0, objects: 0, deletedBytes: 0, deletedObjects: 0, known: true, state: 'recorded' }
        node.children.push(child)
      }
      node = child
    }
  }
  return root
}

export function bucketNodes(plan: PlanSummaryFile, progress: ReadonlyMap<string, ProgressFile>, log: LogSummaryFile | null, live: boolean): BucketNode[] {
  return Object.entries(plan.buckets).map(([name, b]) => {
    const p = progress.get(name), final = log?.buckets[name]
    const decided = p ? Object.values(p.decisions).reduce((a, n) => a + n, 0) : 0
    return {
      name, bytes: b.eligible?.bytes ?? 0, objects: b.eligible?.objects ?? 0,
      deletedBytes: final?.delete_bytes ?? p?.delete_bytes ?? 0,
      deletedObjects: final?.decisions?.delete ?? p?.decisions.delete ?? 0,
      known: !!(p || final),
      state: final?.interrupted ? 'stopped' : final || p?.done ? 'execution complete' : p ? live ? decided >= (b.eligible?.objects ?? Infinity) ? 'finalizing' : 'running' : 'stopped / incomplete' : live ? 'not started' : 'not recorded',
      updated: p?.updated,
    }
  })
}

export function RunBucketProgress({ planned, dir, live, log, fmtBytes, dry, bands = [] }: { planned: PlanSummaryFile; dir: string; live: boolean; log: LogSummaryFile | null; fmtBytes: (n: number) => string; dry: boolean; bands?: RunBand[] }) {
  const names = Object.keys(planned.buckets)
  const qs = useRunFiles<ProgressFile>(names.map(b => `${dir}progress/${b}.json`), live)
  const ps = new Map(names.flatMap((b, i) => qs[i]?.data ? [[b, qs[i].data!] as const] : []))
  const nodes = bucketNodes(planned, ps, log, live)
  const root: BucketNode = { name: 'bucket completion', bytes: nodes.reduce((n, b) => n + b.bytes, 0), objects: nodes.reduce((n, b) => n + b.objects, 0), deletedBytes: nodes.reduce((n, b) => n + b.deletedBytes, 0), deletedObjects: nodes.reduce((n, b) => n + b.deletedObjects, 0), known: nodes.some(n => n.known), state: live ? 'running' : 'final accounting', children: nodes }
  const [area, setArea] = useUrlState('area', stringParam('bytes'))
  const [color, setColor] = useUrlState('mapColor', stringParam('prefix'))
  const isObjects = area === 'objects'
  const deleted = !live && bands.length > 0
  const deletedRoot = useMemo(() => deletionTree(bands), [bands])
  const displayedRoot = deleted ? deletedRoot : root
  const fraction = (n: BucketNode) => Math.max(0, Math.min(1, isObjects ? n.deletedObjects / (n.objects || 1) : n.deletedBytes / (n.bytes || 1)))
  const details = (n: BucketNode) => <div className="tip-viewcard"><b>{n.prefix ?? n.name}</b><span className="dim"> · {n.state}</span><div>{percentOf(n.deletedObjects, n.objects)} objects · {percentOf(n.deletedBytes, n.bytes)} bytes {dry ? 'would delete' : 'acknowledged deleted'}</div><div>{fmtN(n.deletedObjects)} / {fmtN(n.objects)} objects · {fmtBytes(n.deletedBytes)} / {fmtBytes(n.bytes)}</div><div className="dim">Hover for details · click a branch to drill</div></div>
  return <section className="run-buckets">
    <div className="graph-controls"><h3>{deleted ? dry ? 'Predicted prefixes' : 'Deleted prefixes' : 'Bucket completion'}</h3>{['bytes', 'objects'].map(a => <button type="button" className={area === a ? 'on' : ''} key={a} onClick={() => setArea(a)}>area: {a}</button>)}<label>color by <select aria-label="Deletion map colors" value={color} onChange={e => setColor(e.target.value)}>{['prefix', 'bucket', 'completion'].map(c => <option key={c}>{c}</option>)}</select></label></div>
    <p className="dim">{deleted ? `${bands.length} final decision bands. These are logged ${dry ? 'predictions' : 'acknowledgments'}, not a later scan.` : `Colored: ${dry ? 'would delete' : 'acknowledged deleted'} · gray: remainder. Bucket-level totals; skips/failures are not colored. Prefix detail is available after finalization.`}</p>
    <div className="run-bucket-map">
      <Treemap key={deleted ? 'deleted' : 'planned'} root={displayedRoot} getSize={n => isObjects ? n.objects : n.bytes} getChildren={n => n.children} getLabel={n => n.name} getId={n => n.prefix ?? n.name} formatSize={isObjects ? fmtN : fmtBytes} collapseChains
        colorForCell={n => {
          const key = color === 'bucket' ? (n.prefix ?? `gs://${n.name}/`).split('/')[2] : n.prefix ?? n.name
          const index = [...key].reduce((hash, c) => (hash * 31 + c.charCodeAt(0)) >>> 0, 0) % DEFAULT_PALETTE.length
          const fill = color === 'completion' ? '#339a77' : DEFAULT_PALETTE[index]
          return { bg: fraction(n) >= 1 ? fill : '#44443f', ink: '#fff', segments: [{ color: fill, frac: fraction(n) }, { color: '#44443f', frac: 1 - fraction(n) }] }
        }}
        renderCellSubtitle={n => n.known ? percentOf(isObjects ? n.deletedObjects : n.deletedBytes, isObjects ? n.objects : n.bytes) : 'not started'}
        tipMode="dock" renderTooltip={details} renderTipDefault={details}
      />
    </div>
    <div className="runs-wrap"><table className="runs"><thead><tr><th>bucket</th><th>state</th><th className="num">objects</th><th className="num">bytes</th></tr></thead><tbody>{nodes.map(n => <tr key={n.name}><td>{n.name}</td><td><Tooltip content={n.updated ? `Latest progress: ${n.updated}` : 'No progress file for this bucket yet.'}><span>{n.state}</span></Tooltip></td><td className="num">{n.known ? `${fmtN(n.deletedObjects)} / ${fmtN(n.objects)} · ${percentOf(n.deletedObjects, n.objects)}` : `— / ${fmtN(n.objects)}`}</td><td className="num">{n.known ? `${fmtBytes(n.deletedBytes)} / ${fmtBytes(n.bytes)} · ${percentOf(n.deletedBytes, n.bytes)}` : `— / ${fmtBytes(n.bytes)}`}</td></tr>)}</tbody></table></div>
  </section>
}
