import { useCallback, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import { Treemap } from '../Treemap'
import { contrastEdge } from '../colors'
import {
  GREW_GREEN,
  SHRANK_RED,
  UNCHANGED_GREY,
  UNCHANGED_SWATCH,
  TOUCHED_HATCH,
  deltaColor,
  deltaTextColor,
} from './colors'
import { buildDiffTree, fetchable } from './tree'
import type { DiffAreaMode, DiffNode, DiffTreemapProps } from './types'

type Fmt = (n: number | null | undefined) => string

/**
 * `▲ 32.4 ▼ 6.9 Δ +25.5` — churn among a node's descendants, for title
 * strips / crumbs / tooltips. Units are omitted when every value shares the
 * unit of the total it follows (`totalBytes`), and shown per value otherwise
 * (`▲ 100M ▼ 200K Δ +100M`) — never a mixed line the reader has to parse.
 * Both directions when there's churn both ways, else just the Δ.
 */
export function churn(
  n: { grew: number; shrank: number; delta: number },
  /** bytes: the total the line follows (drives unit elision); counts: `null` */
  totalBytes: number | null,
  formatSize: Fmt,
  formatCount: Fmt,
  /** omit the `Δ` part (the caller already shows the net elsewhere) */
  net: boolean = true,
): { text: string; node: ReactNode } | null {
  const both = n.grew > 0 && n.shrank < 0
  if (!both && n.delta === 0) return null
  if (!both && !net) return null
  let fmt: (v: number) => string
  if (totalBytes === null) {
    fmt = (v: number) => formatCount(v)
  } else {
    const vals = both ? [n.grew, -n.shrank, Math.abs(n.delta)] : [Math.abs(n.delta)]
    const unitOf = (b: number) => formatSize(b).split(' ')[1]
    const shared = vals.every(v => v === 0 || unitOf(v) === unitOf(totalBytes))
    fmt = b => shared ? formatSize(b).split(' ')[0] : formatSize(b).replace(' ', '')
  }
  const d = (n.delta > 0 ? '+' : n.delta < 0 ? '−' : '±') + fmt(Math.abs(n.delta))
  const parts: [string, string, string][] = both
    ? [['▲', fmt(n.grew), GREW_GREEN], ['▼', fmt(-n.shrank), SHRANK_RED]]
    : []
  // Counts carry their sign, so the bare number is the net; bytes keep the
  // `Δ` glyph in title strips where it's the only cue the number is a delta.
  if (net) parts.push([totalBytes === null ? '' : 'Δ', d, deltaTextColor(n.delta)])
  return {
    text: parts.map(([g, v]) => (g ? `${g} ${v}` : v)).join(' '),
    node: parts.map(([g, v, color], i) => (
      <span key={g || 'net'} style={{ color, marginLeft: i ? 6 : 0 }}>{g ? `${g} ${v}` : v}</span>
    )),
  }
}

/**
 * Δ-recolor treemap with two area modes:
 *
 * - `max` (default): one cell per row, sized by `max(old, new)` — deleted
 *   subtrees keep their old area, added ones their new area, and unchanged
 *   structure stays visible as neutral context. Color encodes Δ/max per cell:
 *   pure-add is fully red, pure-delete fully green, unchanged neutral.
 *   Caveat (labeled): cell areas sum to more than either side's true total.
 * - `Δ`: the churn view — only changed rows, sized by `|Δbytes|`, colored by
 *   Δ relative to the largest |Δ|.
 *
 * Clicking a directory drills via `onDrill(uri)` so the exploration matches
 * the consumer's deep-link scheme.
 */
export function DiffTreemap({
  input,
  recState,
  onRecRetry,
  onDrill,
  cellHref,
  fetchSubtree,
  formatSize,
  formatCount,
  tiling,
  setTiling,
  mapRef,
  renderContainer,
  renderOverlay,
  renderEmpty,
}: DiffTreemapProps) {
  const [areaMode, setAreaMode] = useState<DiffAreaMode>('max')
  const [showUnchanged, setShowUnchanged] = useState(true)

  const formatDelta = (bytes: number): string => {
    const sign = bytes < 0 ? '−' : '+' // U+2212: a hyphen reads as a dash at small sizes
    return sign + formatSize(Math.abs(bytes)).replace(' ', '')
  }
  const churnFn = (n: { grew: number; shrank: number; delta: number }, totalBytes: number | null, net = true) =>
    churn(n, totalBytes, formatSize, formatCount, net)

  /**
   * Drilling *inside* the widget shows a subtree whose deep cells the server
   * trimmed relative to the whole compared tree — at the subtree's own scale
   * they're big again. So fetch that subtree's slice on drill (one request
   * per drill, cached by the widget), with the floor computed from the map's
   * own pixels: a node's screen area is ≈ its byte share × the canvas.
   */
  const loadChildren = useCallback(
    async (n: DiffNode) => {
      const sub = await fetchSubtree(n)
      const { cells } = buildDiffTree(
        { uri: n.uri, flatRows: [], recRows: sub.recRows, unchangedTop: sub.unchangedTop, unchangedRest: sub.unchangedRest, totalDelta: 0, oldRootSize: 0, newRootSize: 0, oldRootCount: 0, newRootCount: 0 },
        areaMode, showUnchanged,
      )
      return cells
    },
    [fetchSubtree, areaMode, showUnchanged],
  )
  const { root, maxAbsDelta } = useMemo(() => {
    const { cells, maxAbsDelta: maxAbs } = buildDiffTree(input, areaMode, showUnchanged)
    const totalWeight = cells.reduce((s, c) => s + c.weight, 0)
    // Root aggregates its cells so the widget's crumbs line reads correctly.
    const root: DiffNode & { children: DiffNode[] } = {
      key: input.uri,
      label: input.uri,
      weight: totalWeight,
      delta: input.totalDelta,
      grew: cells.reduce((s, c) => s + c.grew, 0),
      shrank: cells.reduce((s, c) => s + c.shrank, 0),
      nGrew: cells.reduce((s, c) => s + c.nGrew, 0),
      nShrank: cells.reduce((s, c) => s + c.nShrank, 0),
      status: 'changed',
      oldSize: input.oldRootSize,
      newSize: input.newRootSize,
      countDelta: input.newRootCount - input.oldRootCount,
      kind: 'dir',
      uri: input.uri,
      children: cells,
    }
    return { root, maxAbsDelta: maxAbs }
  }, [input, areaMode, showUnchanged])

  if (root.children.length === 0) {
    if (renderEmpty) return <>{renderEmpty({ areaMode, showUnchanged, onShowUnchanged: () => setShowUnchanged(true) })}</>
    return (
      <div style={{ padding: 24, textAlign: 'center', color: '#8b949e', fontSize: '0.875rem' }}>
        {areaMode === 'max' && showUnchanged
          ? 'Nothing to plot — no row has any bytes on either side.'
          : 'No size deltas to plot — every row is unchanged.'}
        {!showUnchanged && (
          <div style={{ marginTop: 8 }}>
            <button onClick={() => setShowUnchanged(true)}>show unchanged</button>
          </div>
        )}
      </div>
    )
  }

  const map = (
    // Taller on phones: a 340px strip of a portrait screen is unreadable.
    // (`.dt-diff-map`: height min(75vh,560px), 340px at ≥600px — see styles.css.)
    <div ref={mapRef} className="dt-diff-map">
      <Treemap<DiffNode & { children?: DiffNode[] }>
        root={root}
        getSize={n => n.weight}
        getChildren={n => (n as { children?: DiffNode[] }).children}
        // Only dirs with change *below* have a slice worth fetching.
        hasChildren={fetchable}
        loadChildren={loadChildren}
        renderLoading={n => `Loading Δ for ${n.label || 'this subtree'}…`}
        getLabel={n => n.label}
        // First-class fold cells: aggregate the dust's Δ so a green parent
        // whose change lives entirely in tiny children still shows where
        // (band + tooltip), instead of an inert "(+N)" tile.
        mergeSmall={small => ({
          key: `${small[0].key}__fold${small.length}`,
          label: `(+${small.length})`,
          weight: small.reduce((s, c) => s + c.weight, 0),
          delta: small.reduce((s, c) => s + c.delta, 0),
          grew: small.reduce((s, c) => s + c.grew, 0),
          shrank: small.reduce((s, c) => s + c.shrank, 0),
          nGrew: small.reduce((s, c) => s + c.nGrew, 0),
          nShrank: small.reduce((s, c) => s + c.nShrank, 0),
          status: 'fold',
          oldSize: small.reduce((s, c) => s + c.oldSize, 0),
          newSize: small.reduce((s, c) => s + c.newSize, 0),
          countDelta: small.reduce((s, c) => s + c.countDelta, 0),
          kind: 'fold',
          uri: small[0].uri,
          nFolded: small.reduce((s, c) => s + (c.nFolded ?? 1), 0),
        })}
        // Brighter sibling separation: the compare palette's dark neutrals
        // make the default (transparent) cell rings invisible.
        // Tiling per the header toggle (shared: exact areas, the stroke is
        // the boundary; gaps: classic gutters). The dark compare palette
        // needs a light stroke/ring either way.
        tiling={tiling}
        // The stroke paints over each cell's opaque base (the container
        // color), so it's one fixed color for the whole map: mid grey reads
        // against both the bright full-Δ cells and the dark mostly-grey
        // ones (a darker stroke disappeared into the latter).
        mapStyle={{
          '--dt-treemap-edge': 'rgba(255, 255, 255, 0.34)',
          '--dt-treemap-cell-border': 'rgba(255, 255, 255, 0.14)',
        } as CSSProperties}
        // Displayed inline with the label — the raw |Δ| magnitude. Sign is
        // encoded by the cell color; exact old/new/Δ lives in the tooltip.
        formatSize={formatSize}
        colorForCell={(n, _path, _depth, ctx) => {
          // Stroke per cell, from the face it borders (see `contrastEdge`).
          const withEdge = (s: { bg: string; ink: string; hatch?: string }, face: string) =>
            ({ ...s, edge: contrastEdge(face, ctx.fade) ?? undefined })
          // A branch either renders nested tiles now (`ctx.hasKids`) or
          // holds children the layout was too small to draw — both take the
          // container treatment, not the leaf band. (Lazily-loaded subtrees
          // render kids without `n.children`, hence both halves.)
          const hasKids = ctx.hasKids || !!n.children?.length
          if (areaMode === 'max') {
            if (hasKids) {
              // Parent: children tile its interior, so only the title strip
              // and gutters show — tint them by the net trend Δ/weight (a
              // summary cue; magnitude lives in the leaf bands). Net-zero
              // parents get the same grey as every other unchanged rect.
              if (n.delta === 0) return withEdge({ bg: UNCHANGED_GREY, ink: '#fff', ...(n.status === 'touched' && { hatch: TOUCHED_HATCH }) }, UNCHANGED_GREY)
              const t = n.weight === 0 ? 0 : n.delta / n.weight
              return withEdge({ bg: deltaColor(t), ink: '#fff' }, deltaColor(t))
            }
            // Sub-rect encoding: a grey rect of min(old, new) bytes plus a
            // full-strength colored band of |Δ| bytes, filling from the
            // bottom — magnitude by *area*, not saturation.
            const f = n.weight === 0 ? 0 : Math.min(1, Math.abs(n.delta) / n.weight)
            if (f === 0) return withEdge({ bg: UNCHANGED_GREY, ink: '#fff', ...(n.status === 'touched' && { hatch: TOUCHED_HATCH }) }, UNCHANGED_GREY)
            const pct = `${(f * 100).toFixed(2)}%`
            const band = deltaColor(Math.sign(n.delta))
            return withEdge({
              bg: `linear-gradient(to top, ${band} ${pct}, ${UNCHANGED_GREY} ${pct})`,
              ink: '#fff', // uniform with every other label
            // The stroke follows whichever half dominates the face.
            }, f > 0.5 ? band : UNCHANGED_GREY)
          }
          // Δ mode: full cell tinted by Δ relative to the largest |Δ|.
          if (n.delta === 0) return withEdge({ bg: UNCHANGED_GREY, ink: '#fff' }, UNCHANGED_GREY)
          const t = maxAbsDelta === 0 ? 0 : n.delta / maxAbsDelta
          return withEdge({ bg: deltaColor(t), ink: '#fff' }, deltaColor(t))
        }}
        renderCellExtra={areaMode === 'max' ? (n, _path, { w, h, hasKids }) => {
          const branch = hasKids || !!n.children?.length
          // Per-sub-rect size labels: Δ centered in the colored band, the
          // unchanged min(old, new) bytes centered in the grey rect above it.
          // Leaves only (a parent's interior belongs to its children), and
          // skip narrow slivers — a clipped "+128.0KB" is worse than none.
          if (branch || w < 56) return null
          const f = n.weight === 0 ? 0 : Math.min(1, Math.abs(n.delta) / n.weight)
          if (f === 0) return null
          const bandH = h * f
          const greyH = h - bandH
          // The title strip owns the top ~20px; a band label whose band
          // reaches into it collides with the name in short cells.
          if (greyH < 22 && h < 44) return null
          const minBytes = Math.min(n.oldSize, n.newSize)
          const lbl = (top: string, height: number, text: string, style: CSSProperties) => (
            <div style={{
              position: 'absolute', top, left: 0, right: 0, height,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              pointerEvents: 'none', fontSize: '0.75rem', ...style,
            }}>{text}</div>
          )
          return (
            <>
              {bandH >= 16 && lbl(`${(100 - f * 100).toFixed(2)}%`, bandH,
                formatDelta(n.delta), { color: '#fff', fontWeight: 600 })}
              {f < 1 && greyH >= 44 && minBytes > 0 && lbl('0', greyH,
                formatSize(minBytes), { color: 'var(--dt-treemap-ink, #d0d0d8)', opacity: 0.65 })}
            </>
          )
        } : undefined}
        // Branch title strips have room to spare: inline the ▲/▼/Δ (or
        // just the Δ) when it fits after the name and size — a rough
        // char-width estimate against the cell box, since the size span
        // never shrinks and would crush the name otherwise.
        renderCellSubtitle={(n, _path, { w, hasKids }) => {
          if (!hasKids && !n.children?.length) return null
          // ▲/▼/Δ glyphs run wider than digits; err toward dropping the
          // stats over ellipsizing the name.
          const room = w - 8 - n.label.length * 7 - formatSize(n.weight).length * 6.5 - 12
          const fits = (c: { text: string }) => c.text.length * 7.5 + 12 <= room
          const full = churnFn(n, n.weight)
          if (!full) return null
          if (fits(full)) return full.node
          const net = churnFn({ grew: 0, shrank: 0, delta: n.delta }, n.weight)
          return net && fits(net) ? net.node : null
        }}
        // The crumbs row is the drilled node's own "title strip".
        renderCrumbSuffix={n => (
          <>— {formatSize(n.weight)}{(() => { const c = churnFn(n, n.weight); return c && <> {c.node}</> })()}</>
        )}
        renderTooltip={n => (
          <>
            <div style={{ fontWeight: 500 }}>
              {n.status === 'fold'
                ? `${(n.nFolded ?? 0).toLocaleString()} smaller items`
                : n.status === 'filler'
                ? (n.nRest === undefined ? 'unchanged' : `${n.nRest.toLocaleString()} unchanged children`)
                : n.label || 'unchanged'}
            </div>
            <div style={{ color: 'rgba(255, 255, 255, 0.75)', fontSize: '0.85em' }}>
              {n.delta === 0
                ? formatSize(n.newSize)
                : <>{formatSize(n.oldSize)} → {formatSize(n.newSize)} (<span style={{ color: deltaTextColor(n.delta), fontWeight: 600, fontSize: '1.1em' }}>{formatDelta(n.delta)}</span>)</>}
              {n.status === 'unchanged' && ' — unchanged'}
              {n.status === 'touched' && ' — touched (same bytes & count, mtime moved)'}
              {n.status === 'filler' && n.nDescRest !== undefined && n.nDescRest !== n.nRest && (
                <> · {formatCount(n.nDescRest)} entries below</>
              )}
            </div>
            {/* Aggregates with churn in both directions: the net alone
                hides how much grew vs shrank among descendants. */}
            {n.grew > 0 && n.shrank < 0 && (
              <div style={{ fontSize: '0.8em' }}>{churnFn(n, n.newSize, false)?.node}</div>
            )}
            {/* Entry counts get the same ▲/▼/Δ treatment as bytes. */}
            {(n.countDelta !== 0 || n.nGrew > 0) && (
              <div style={{ fontSize: '0.8em' }}>
                <span style={{ opacity: 0.6 }}>count </span>
                {churnFn({ grew: n.nGrew, shrank: n.nShrank, delta: n.countDelta }, null)?.node}
              </div>
            )}
            {/* Only what the size line doesn't already say. */}
            {(n.status === 'filler' || n.status === 'fold' || n.status === 'added' || n.status === 'removed' || (n.pruned && input.index?.status !== 'done')) && (
              <div style={{ opacity: 0.5, fontSize: '0.75em', marginTop: 2 }}>
                {n.status === 'filler' ? 'unchanged bytes the diff never needed to enumerate'
                  : n.status === 'fold' ? 'children too small to draw, aggregated'
                  : n.status === 'added' || n.status === 'removed' ? n.status
                  : null}
                {n.pruned && input.index?.status !== 'done' && <>
                  {(n.status === 'added' || n.status === 'removed') && ' · '}
                  {n.delta === 0 && n.countDelta === 0
                    ? 'not descended (walk budget): something inside moved — click to compare here'
                    : 'Δ not localized (walk budget) — click to compare here'}
                </>}
              </div>
            )}
          </>
        )}
        renderLegend={() => (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '0.8rem', opacity: 0.85 }}>
            {/* Δ share key: how much of a cell's max(old, new) the change
                is — the same ramp `deltaColor` walks, so a hue reads back
                as a rough percentage. */}
            <span
              title="Cell color = Δ as a share of max(old, new): full color when the change is the whole cell, grey when it's a sliver. Leaves paint that share as a band from the bottom; branches tint their title strip by the net share."
              style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}
            >
              <span style={{ opacity: 0.6 }}>−100%</span>
              <span style={{
                display: 'inline-block', width: 84, height: 10, borderRadius: 2,
                background: `linear-gradient(to right, ${deltaColor(-1)}, ${deltaColor(-0.4)}, ${UNCHANGED_SWATCH}, ${deltaColor(0.4)}, ${deltaColor(1)})`,
              }} />
              <span style={{ opacity: 0.6 }}>+100%</span>
            </span>
            {areaMode === 'max' && (
              // Legend-item toggle (plot-legend style): click to hide/show
              // unchanged rows. Hidden = only changed entries plot (at
              // max(old, new) size); parents shrink to their changed
              // contents, fillers and grey context rows drop out.
              <button
                onClick={e => { e.stopPropagation(); setShowUnchanged(s => !s) }}
                title={showUnchanged
                  ? 'Hide unchanged rows: only changed entries plot; parents shrink to their changed contents'
                  : 'Show unchanged rows: grey context cells and fillers restore true directory proportions'}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer',
                  background: 'none', border: 'none', padding: 0, margin: 0,
                  font: 'inherit', color: 'inherit',
                  opacity: showUnchanged ? 1 : 0.45,
                  textDecoration: showUnchanged ? 'none' : 'line-through',
                }}
              >
                {/* The map's grey is translucent; on the bar's own dark
                    background it needs its composited color to be seen. */}
                <span style={{ display: 'inline-block', width: 12, height: 12, background: UNCHANGED_SWATCH, border: '1px solid rgba(255,255,255,0.25)', borderRadius: 2, boxSizing: 'border-box' }} />
                unchanged
              </button>
            )}
            {areaMode === 'max' && (
              <span title="same bytes & count, mtime moved (rename / net-zero churn / touch)" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <span style={{ display: 'inline-block', width: 12, height: 12, background: UNCHANGED_SWATCH, backgroundImage: TOUCHED_HATCH, border: '1px solid rgba(255,255,255,0.25)', borderRadius: 2, boxSizing: 'border-box' }} />
                touched
              </span>
            )}
            {input.index && input.index.status !== 'done' && (
              <span
                title={input.index.status === 'failed'
                  ? `full diff index failed: ${input.index.error ?? 'unknown error'} — showing the budgeted walk`
                  : 'showing a budgeted walk while the full diff index builds; the map completes itself when it lands'}
                style={{ opacity: 0.6, marginLeft: 6, fontStyle: 'italic' }}
              >
                {input.index.status === 'failed' ? 'index failed' : 'indexing…'}
              </span>
            )}
            <span style={{ display: 'inline-flex', gap: 2, marginLeft: 6 }}>
              {(['max', 'delta'] as const).map(m => (
                <button
                  key={m}
                  onClick={e => { e.stopPropagation(); setAreaMode(m) }}
                  title={m === 'max'
                    ? 'Area = max(old, new), with |Δ| painted as a band: deleted subtrees keep their old area; stable structure stays visible'
                    : 'Area = |Δbytes|: churn only, unchanged rows dropped'}
                  style={{
                    cursor: 'pointer', fontSize: '0.75rem', padding: '1px 7px', borderRadius: 3,
                    border: '1px solid var(--dt-border, #444)',
                    background: areaMode === m ? 'var(--dt-accent-bg, #30363d)' : 'transparent',
                    color: 'inherit', fontWeight: areaMode === m ? 600 : 400,
                  }}
                >
                  {m === 'max' ? 'max' : 'Δ'}
                </button>
              ))}
            </span>
            {/* Tiling sits with the other view controls, not in the app
                header — it's the same kind of knob as max/Δ. */}
            <span style={{ display: 'inline-flex', gap: 2, marginLeft: 4 }}>
              {(['gaps', 'shared'] as const).map(t => (
                <button
                  key={t}
                  onClick={e => { e.stopPropagation(); setTiling(t) }}
                  title={t === 'gaps'
                    ? '2px gutters and rounded corners; dense leaf fields under-paint by ~perimeter/area'
                    : 'Cells abut, one stroke per boundary — areas exact (a 6×6px cell with 2px gutters paints only 4×4)'}
                  style={{
                    cursor: 'pointer', fontSize: '0.75rem', padding: '1px 7px', borderRadius: 3,
                    border: '1px solid var(--dt-border, #444)',
                    background: tiling === t ? 'var(--dt-accent-bg, #30363d)' : 'transparent',
                    color: 'inherit', fontWeight: tiling === t ? 600 : 400,
                  }}
                >
                  {t}
                </button>
              ))}
            </span>
          </span>
        )}
        onCellClick={(n) => {
          // Option A (matches the scan treemap): a dir click re-roots the
          // whole /compare page — URL + breadcrumb + table + map — preserving
          // scan1/scan2, rather than drilling the map in place and diverging
          // from the table. Synthetic filler/fold cells (no real uri) and
          // files keep the widget default (pin tooltip).
          if (n.kind !== 'dir' || n.status === 'filler' || n.status === 'fold') return false
          onDrill(n.uri)
          return true
        }}
        cellHref={n =>
          n.kind === 'dir' && n.status !== 'filler' && n.status !== 'fold' && n.uri
            ? cellHref?.(n.uri)
            : undefined
        }
      />
      {/* The colored Δ cells come from the recursive frontier; until it
          lands, the flat grey context alone reads as a broken all-grey
          map — put the state front and center, not in the legend. */}
      {recState !== 'ready' && (renderOverlay
        ? renderOverlay(recState, onRecRetry)
        : (
          <div
            style={{
              position: 'absolute', inset: 0, zIndex: 2,
              display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
              gap: 12, background: 'rgba(8, 10, 12, 0.55)',
              pointerEvents: recState === 'error' ? 'auto' : 'none',
            }}
          >
            {recState === 'loading'
              ? <span style={{ opacity: 0.9 }}>computing Δ detail…</span>
              : <button onClick={onRecRetry}>Δ detail failed — retry</button>}
          </div>
        ))}
    </div>
  )

  return <>{renderContainer ? renderContainer(map) : map}</>
}
