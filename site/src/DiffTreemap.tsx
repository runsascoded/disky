import { Explain } from './Help'
import { useMemo } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { Treemap as DtTreemap, divergingColor, divergingInk } from '@disk-tree/react'
import { stringParam, useUrlState } from 'use-prms'
import { useRenderer, useTiling } from './prefs'
import { useUnits } from './units'
import { buildTree } from './diffModel'
import { diffCellAction } from './diffRows'
import type { AreaMode, DiffData, DiffNode } from './diffModel'
import { usePerfCommit } from './perf'

const { abs, max, min, sign } = Math

// Ported from disk-tree's CompareView (ui/src/components/CompareView.tsx):
// `divergingColor` is red-positive, so negate on the way in.
const UNCHANGED_GREY = 'rgba(110, 118, 129, 0.28)'
const deltaColor = (t: number) => divergingColor(-t)
// A root the older scan never covered (specs/root-geneses.md §3): neither
// grew nor shrank, so neither green nor red — the site's blue. A first-scanned
// bucket AND everything under it read blue: its bytes entered the scan, they
// aren't the interval's writes, so painting its interior green (as "added")
// would double-count the day's growth. Blue frames a blue interior — never a
// blue box sitting under green children.
const FIRST_SCANNED = 'var(--s1)'

/** The diff's derived model: the built tree, the resolved area mode, the root
 *  movement totals, and the unit-aware formatters — computed once and shared by
 *  the header band (DiffHeader, above the map) and the map (DiffTreemap). Lifted
 *  out of the map so the header (scan pickers + stats + legend) stays mounted
 *  while a diff is loading or errored, when the map itself isn't rendered. */
export type DiffModel = {
  data: DiffData
  root: DiffNode
  areaMode: AreaMode
  setAreaMode: (m: AreaMode) => void
  label: string
  added: number
  removed: number
  n_added: number
  n_removed: number
  grew: number
  grewN: number
  firstScanned: number
  firstScannedN: number
  fmtBytes: (n: number) => string
  fmtDelta: (d: number) => string
  fmtN: (n: number) => string
  fmtNDelta: (d: number) => string
  fmtC: (n: number) => string
  fmtCDelta: (d: number) => string
}

export function useDiffModel(data: DiffData | null, atRoot: boolean, label: string): DiffModel | null {
  const { fmtBytes } = useUnits()
  // Area mode is shareable state: `?dm=max` switches to max(old,new) areas; Δ
  // (area = |delta|) is the default — the movement is what a diff view is for —
  // and stays out of the URL.
  const [dmP, setDmP] = useUrlState('dm', stringParam())
  const setAreaMode = (m: AreaMode) => setDmP(m === 'max' ? 'max' : undefined)
  const derived = useMemo(() => {
    if (!data) return null
    const fmtDelta = (d: number) => (d >= 0 ? '+' : '−') + fmtBytes(abs(d))
    const fmtN = (n: number) => n.toLocaleString('en-US')
    const fmtNDelta = (d: number) => (d >= 0 ? '+' : '−') + fmtN(abs(d))
    // The root movement table gets compact sig-figs on objects (5.63M, not
    // 5,634,588) — the exact digit is never the point there. Per-cell tooltips
    // keep exact counts (you're inspecting one).
    const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumSignificantDigits: 3 })
    const fmtC = (n: number) => compact.format(n)
    const fmtCDelta = (d: number) => (d >= 0 ? '+' : '−') + fmtC(abs(d))
    // Area mode falls back to `max` when Δ has nothing to show: if the user
    // hasn't pinned a mode and the movement tree is empty (nothing moved between
    // the two scans), draw the sizes in `max` instead of a bare "no changes".
    const wantMax = dmP === 'max'
    let mode: AreaMode = wantMax ? 'max' : 'delta'
    let { cells } = buildTree(data, mode, atRoot)
    if (!wantMax && cells.length === 0) {
      const alt = buildTree(data, 'max', atRoot)
      if (alt.cells.length) { mode = 'max'; cells = alt.cells }
    }
    const root: DiffNode = {
      key: label,
      label,
      k: 'dir',
      weight: cells.reduce((s, c) => s + c.weight, 0),
      delta: data.total_b - data.total_a,
      added: cells.reduce((s, c) => s + c.added, 0),
      removed: cells.reduce((s, c) => s + c.removed, 0),
      status: 'root',
      size_old: data.total_a,
      size_new: data.total_b,
      n_desc_delta: data.objects_b - data.objects_a,
      n_old: data.objects_a,
      n_new: data.objects_b,
      n_added: cells.reduce((s, c) => s + c.n_added, 0),
      n_removed: cells.reduce((s, c) => s + c.n_removed, 0),
      children: cells,
    }
    if (!root.children?.length) return null
    const { added, removed, n_added, n_removed } = root
    // Roots that entered the scan in this interval: shown apart from the
    // interval's writes (`⊕ first scanned`), so the day's growth stays readable.
    const firstScanned = (root.children ?? []).filter(c => c.first).reduce((s, c) => s + c.added, 0)
    const firstScannedN = (root.children ?? []).filter(c => c.first).reduce((s, c) => s + c.n_added, 0)
    return {
      root, areaMode: mode, added, removed, n_added, n_removed,
      firstScanned, firstScannedN, grew: added - firstScanned, grewN: n_added - firstScannedN,
      fmtBytes, fmtDelta, fmtN, fmtNDelta, fmtC, fmtCDelta,
    }
  }, [data, dmP, atRoot, label, fmtBytes])
  if (!derived || !data) return null
  return { data, label, setAreaMode, ...derived }
}

/** The 2-row header band above the diff map: scan pickers + presets (passed in
 *  as `controls`) with the colour legend beneath them, the bytes/objects
 *  movement table beside them, and the area-mode toggle in the top-right corner
 *  (by the map's fullscreen button). Renders the controls alone when the model
 *  isn't ready (diff loading / errored), so the pickers never disappear. */
export function DiffHeader({ model, controls }: { model: DiffModel | null; controls: ReactNode }) {
  return (
    <div className="diff-head">
      <div className="dh-left">
        <div className="dh-controls">{controls}</div>
        {model && (
          <div className="dh-legend">
            <span className="sw" style={{ background: deltaColor(1) }} /> grew
            <span className="sw" style={{ background: deltaColor(-1) }} /> shrank
            {model.firstScanned > 0 && <>
              <span className="sw" style={{ background: FIRST_SCANNED }} /> first scanned
            </>}
            {model.areaMode === 'max' && <>
              <span className="sw" style={{ background: UNCHANGED_GREY }} /> unchanged
            </>}
          </div>
        )}
      </div>
      {model && <DiffModes model={model} />}
    </div>
  )
}

/** One movement row: `start − removed + added (⊕ first) = end (±Δ)`, in the
 *  shared `.diff-move` columns so rows line up. */
interface MoveRow {
  label: string
  old: number; removed: number; added: number; neu: number; delta: number; first?: number
  fmt: (n: number) => string; fmtD: (n: number) => string
  eq?: string; suffix?: string
}

/** The movement table (bytes + objects), used for both the whole-diff totals
 *  (resting drawer) and a hovered cell — one format + font everywhere. Columns
 *  are always shown (even a `− 0`), so a cell reads the same as the root. */
function MoveTable({ rows, showFirst }: { rows: MoveRow[]; showFirst?: boolean }) {
  return (
    <table className="diff-move">
      <tbody>
        {rows.map(r => (
          <tr key={r.label}>
            <th>{r.label}</th>
            <td>{r.fmt(r.old)}</td>
            <td className="shrank">− {r.fmt(r.removed)}</td>
            <td className="grew">+ {r.fmt(r.added)}</td>
            {showFirst && <td className="first">{r.first ? `⊕ ${r.fmt(r.first)}` : ''}</td>}
            <td className="eq">{r.eq ?? '='} {r.fmt(r.neu)}{r.suffix ?? ''}</td>
            <td className={r.delta >= 0 ? 'grew' : 'shrank'}>({r.fmtD(r.delta)})</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** The root movement table: bytes and objects (compact sig-figs). */
function DiffStats({ model }: { model: DiffModel }) {
  const { data, root, added, removed, n_added, n_removed, grew, grewN, firstScanned, firstScannedN,
    fmtBytes, fmtDelta, fmtC, fmtCDelta } = model
  const bEq = data.truncated || added - removed !== root.delta ? '≈' : '='
  const nEq = data.truncated || n_added - n_removed !== root.n_desc_delta ? '≈' : '='
  return (
    <MoveTable showFirst={firstScanned > 0} rows={[
      { label: 'bytes', old: root.size_old, removed, added: grew, neu: root.size_new, delta: root.delta, first: firstScanned, fmt: fmtBytes, fmtD: fmtDelta, eq: bEq },
      { label: 'objects', old: root.n_old, removed: n_removed, added: grewN, neu: root.n_new, delta: root.n_desc_delta, first: firstScannedN, fmt: fmtC, fmtD: fmtCDelta, eq: nEq },
    ]} />
  )
}

/** Area-mode toggle (max vs Δ), parked top-right by the fullscreen button. */
function DiffModes({ model }: { model: DiffModel }) {
  const { areaMode, setAreaMode } = model
  return (
    <div className="dh-modes">
      {(['max', 'delta'] as const).map(m => (
        <Explain key={m} text={m === 'max'
          ? 'Cell area = max(old, new) bytes, with a band for |Δ| — what is there, and how much of it moved'
          : 'Cell area = |Δ| bytes, colour = Δ as a share of the directory — only the movement'}>
          <button
            onClick={e => { e.stopPropagation(); setAreaMode(m) }}
            className={'diff-mode' + (areaMode === m ? ' on' : '')}
          >
            {m === 'max' ? 'max' : 'Δ'}
          </button>
        </Explain>
      ))}
    </div>
  )
}

export function DiffTreemap({ model, onDrill, onOpen }: {
  model: DiffModel
  /** A directory cell was drilled: its path segments relative to the diff's
   *  scope. The page drills there (and this diff re-reads at that prefix), so
   *  the map never holds a drill of its own. */
  onDrill?: (segs: string[]) => void
  /** An object cell was clicked: its segments relative to the diff's scope
   *  (the page opens it in the leaf viewer). */
  onOpen?: (segs: string[]) => void
}) {
  usePerfCommit('dtm')
  const { root, areaMode, label, fmtBytes, fmtDelta, fmtN, fmtNDelta } = model
  // The main map's renderer / tiling prefs apply here too.
  const [tiling] = useTiling()
  const [renderer] = useRenderer()

  // Rect seams like the main map: a fat gutter at the top level, a clear line
  // one down, hairlines below — so nested cells read as nested, not as one flat
  // mosaic. Colours: the treemap's contrast half-stroke (blue for first-scanned).
  const borderWidth = (depth: number, { w, h }: { w: number; h: number }): number => {
    const base = depth === 0 ? 3 : depth === 1 ? 2 : 1
    return min(base, max(1, min(w, h) / 16))
  }

  return (
    <div className="diff-tm">
      <DtTreemap<DiffNode>
        root={root}
        // Controlled at its root: a drill is the page's, not this map's. `k`
        // decides (`diffCellAction`): an object opens, a directory drills —
        // with or without enumerated children (the page's own read brings
        // them) — and a fold or filler pins its tip.
        path={[root]}
        onCellClick={(n, _p, e) => {
          const act = diffCellAction(n, e.altKey)
          const go = act === 'open' ? onOpen : act === 'drill' ? onDrill : undefined
          if (!go) return false
          go(n.key.split('/'))
          return true
        }}
        getSize={n => n.weight}
        getChildren={n => n.children}
        getLabel={n => (n.first ? `${n.label} (first scanned)` : n.label)}
        getId={(_n, p) => p.map(x => x.key).join('|')}
        formatSize={n => fmtBytes(n)}
        tipMode="dock"
        // Crumb and movement totals both live in the header band above the map
        // now, so the map's own crumb suffix is empty.
        renderCrumbSuffix={() => null}
        collapseChains
        depthFade={1}
        rootFade={1}
        borderWidth={borderWidth}
        tiling={tiling}
        renderer={renderer}
        colorForCell={n => {
          // First-scanned: the bucket and its whole subtree read blue. A
          // container gets a neutral fill with a blue frame (the borders draw
          // it), so its blue children aren't sitting inside a blue block; a
          // leaf takes the blue fill.
          if (n.fs) {
            return n.children?.length
              ? { bg: 'color-mix(in oklab, var(--s1) 28%, var(--panel))', ink: 'var(--ink)', edge: FIRST_SCANNED }
              : { bg: FIRST_SCANNED, ink: '#fff', edge: FIRST_SCANNED }
          }
          // No `edge` below: the treemap's adaptive contrast half-stroke (dark on
          // light fills, light on dark) draws the seams, as on the main map — a
          // fill-tinted edge left green-on-green cells without a visible border.
          if (areaMode === 'max') {
            if (n.children?.length) {
              const t = n.weight === 0 ? 0 : n.delta / n.weight
              const bg = deltaColor(t)
              return { bg, ink: divergingInk(t) }
            }
            const f = n.weight === 0 ? 0 : min(1, abs(n.delta) / n.weight)
            if (f === 0) return { bg: UNCHANGED_GREY, ink: divergingInk(0) }
            const pct = `${(f * 100).toFixed(2)}%`
            const band = deltaColor(sign(n.delta))
            return {
              bg: `linear-gradient(to top, ${band} ${pct}, ${UNCHANGED_GREY} ${pct})`,
              ink: divergingInk(f > 0.85 ? 1 : 0),
            }
          }
          // Δ mode: area already says how much moved; color says how much of the
          // node that was — a wholly added / removed directory is full green /
          // red however small, a 5% shrink is a faint red, a net-zero churn is
          // grey (its tooltip shows the churn).
          const base = max(n.size_old, n.size_new)
          const t = base === 0 ? 0 : n.delta / base
          const bg = deltaColor(t)
          return { bg, ink: divergingInk(t) }
        }}
        renderCellExtra={areaMode === 'max' ? (n, _path, { w, h }) => {
          if (n.fs || n.children?.length || w < 56) return null
          const f = n.weight === 0 ? 0 : min(1, abs(n.delta) / n.weight)
          if (f === 0) return null
          const bandH = h * f
          const greyH = h - bandH
          const minBytes = min(n.size_old, n.size_new)
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
                fmtDelta(n.delta), { color: '#fff', fontWeight: 600 })}
              {f < 1 && greyH >= 44 && minBytes > 0 && lbl('0', greyH,
                fmtBytes(minBytes), { color: 'var(--ink-2)', opacity: 0.65 })}
            </>
          )
        } : undefined}
        renderTooltip={n => (
          <>
            <div style={{ fontWeight: 500 }}>{n.key}{n.first ? ' (first scanned)' : ''}</div>
            <MoveTable rows={[
              { label: 'bytes', old: n.size_old, removed: n.removed, added: n.added, neu: n.size_new, delta: n.delta, fmt: fmtBytes, fmtD: fmtDelta },
              ...(n.status !== 'filler'
                ? [{ label: 'objects', old: n.n_old, removed: n.n_removed, added: n.n_added, neu: n.n_new, delta: n.n_desc_delta, fmt: fmtN, fmtD: fmtNDelta, suffix: ' obj' }]
                : []),
            ]} />
            <div style={{ opacity: 0.5, fontSize: '0.75em', marginTop: 2 }}>
              {n.status === 'filler' ? 'unchanged bytes the diff never needed to enumerate'
                : n.fs ? 'first scanned — entered the scan this interval, not written in it'
                : n.status}
              {n.lookup && ' · under the floor on one side, read exactly'}
            </div>
          </>
        )}
        // Resting card (nothing hovered / on a phone): the whole diff's totals
        // as the full start − removed + added = end (±Δ) breakdown. This is the
        // one home for them (the header band no longer duplicates it), and it's
        // always present, so the numbers never disappear.
        renderTipDefault={() => (
          <div className="tip-viewcard">
            <div className="vc-scope">{label}</div>
            <DiffStats model={model} />
            <div className="vc-hint">Hover a cell for its movement.</div>
          </div>
        )}
      />
    </div>
  )
}
