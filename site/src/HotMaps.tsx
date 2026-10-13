import { Treemap } from '@rdub/treemap'
import { useEffect, useMemo, useState } from 'react'
import { exactInteger, type HotResult } from './hotModel'
import { hotMap, type HotMapNode, type HotMapSnapshot } from './hotTreemap'
import { useUnits } from './units'
import { fmtBytesPrecise } from './types'
import '@rdub/treemap/styles.css'

export function HotSnapshotMap({ snapshot, noun = 'bucket' }: { snapshot: HotMapSnapshot; noun?: string }) {
  const { units, suffixB } = useUnits()
  const fmtBytes = (bytes: number) => fmtBytesPrecise(bytes, units, suffixB)
  const [hover, setHover] = useState<HotMapNode | null>(null)
  const [pinned, setPinned] = useState<HotMapNode | null>(null)
  useEffect(() => {
    const clear = (event: KeyboardEvent) => { if (event.key === 'Escape') setPinned(null) }
    window.addEventListener('keydown', clear)
    return () => window.removeEventListener('keydown', clear)
  }, [])
  const selected = pinned ?? hover
  return <figure className="hot-snapshot-map">
    <figcaption>{snapshot.date}<span>{fmtBytes(snapshot.root.b)} matching bytes</span></figcaption>
    {snapshot.empty ? <div className="hot-map-empty">{snapshot.empty}</div> : <>
      <div className="hot-map-canvas"><Treemap<HotMapNode>
        root={snapshot.root}
        getSize={node => node.b}
        getChildren={node => node.children}
        getLabel={node => node.path}
        getId={node => node.path}
        formatSize={fmtBytes}
        colorForCell={node => ({ bg: node.color, ink: '#101010' })}
        renderer="dom"
        chrome={false}
        fullscreen={false}
        tiling="shared"
        rootFade={1}
        minCellArea={null}
        minCellSide={null}
        mapStyle={{ height: '100%' }}
        className="hot-bucket-map"
        renderTooltip={() => null}
        onCellHover={node => setHover(node)}
        onCellClick={node => {
          setPinned(current => current?.path === node.path ? null : node)
          return true
        }}
      /></div>
      <div className="hot-map-detail" aria-live="polite">
        {selected ? <><strong>{selected.path}</strong><span>{exactInteger(selected.b)} bytes; {exactInteger(selected.o)} objects</span>
          {pinned && <button type="button" aria-label={`Clear selected ${noun}`} onClick={() => setPinned(null)}>Clear</button>}
        </> : <span>Hover a {noun} or press Enter on it for exact totals. No deeper drill-down.</span>}
      </div>
    </>}
  </figure>
}

export function HotMaps({ result }: { result: HotResult }) {
  const snapshots = useMemo(() => (result.before ? [result.before, result.after] : [result.after]).map(hotMap), [result])
  return <section className="hot-maps" aria-label="Matching bytes by bucket">
    <h2>Matching bytes by bucket</h2>
    <p>“{result.after.pattern}”{result.before ? ` from ${result.before.date} to ${result.after.date}` : ` on ${result.after.date}`}</p>
    <div className={'hot-map-pair' + (result.before ? ' comparison' : '')}>
      {snapshots.map(snapshot => <HotSnapshotMap key={`${snapshot.date}:${snapshot.pattern}`} snapshot={snapshot} />)}
    </div>
    <p className="hot-note">Each map fills its own snapshot's byte total; areas show bucket shares, not change or object counts. Colors stay the same across scans. Zero-byte objects have no area; the exact table below retains all counts.</p>
  </section>
}
