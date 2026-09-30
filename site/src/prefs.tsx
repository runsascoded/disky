import { Explain } from './Help'
import { useEffect, useRef, useSyncExternalStore } from 'react'
import type { Tiling } from '@disk-tree/react'
import { Tooltip } from './Tooltip'

// Treemap rendering preferences — per-browser, persisted in localStorage,
// read by every treemap on the site. Same module-store + useSyncExternalStore
// shape as upstream disk-tree's `ui/src/utils/tiling.ts`.
//
// - `tiling`: `shared` (cells share edges — one stroke per boundary, exact
//   areas; the default) vs `gaps` (2px gutters, rounded corners).
// - `renderer`: `dom` (one element per cell; the default, full feature
//   parity) vs `canvas` (one paint loop for the whole map — for the 1e3–1e6
//   cell views the DOM renderer bogs down on; per-cell React extras — the
//   actor badges — don't draw there yet).
export type Renderer = 'dom' | 'canvas'

function pref<T extends string>(key: string, ok: readonly T[], dflt: T) {
  const KEY = `gcs-usage:${key}`
  const load = (): T => {
    try {
      const v = localStorage.getItem(KEY)
      if (v && (ok as readonly string[]).includes(v)) return v as T
    } catch { /* no storage */ }
    return dflt
  }
  let current: T = load()
  const listeners = new Set<() => void>()
  const get = () => current
  const set = (t: T): void => {
    if (t === current) return
    current = t
    try { localStorage.setItem(KEY, t) } catch { /* in-memory only */ }
    listeners.forEach(l => l())
  }
  const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
  const use = (): [T, (t: T) => void] => [useSyncExternalStore(subscribe, get, get), set]
  return { use, set }
}

const tiling = pref<Tiling>('tiling', ['shared', 'gaps'], 'shared')
const renderer = pref<Renderer>('renderer', ['dom', 'canvas'], 'dom')
// The help line (specs/edu-drawer.md): on until the reader turns it off.
const help = pref<'on' | 'off'>('help', ['on', 'off'], 'on')
export const useHelpPref = help.use
export const setHelpPref = help.set
export const useTiling = tiling.use
export const setTiling = tiling.set
export const useRenderer = renderer.use

const TILING_TIP = 'Cell gutters. Off (default): cells share edges — one stroke per boundary, areas stay exact. On: gaps and rounded corners between cells.'
const RENDERER_TIP = 'DOM (default): one element per cell — every feature, keyboard focus on every cell. Canvas: the whole map painted in one pass, for views of thousands of cells that make the DOM renderer crawl; outlines, tooltips and drilling work the same, but the marker avatars on cells don’t draw there yet.'

/** Map-display preferences behind a ⚙ (in the treemap's key line): tiling
 * gutters and the renderer. A native `<details>`, so it closes on Esc /
 * toggle and needs no outside-click plumbing. */
export function SettingsMenu() {
  const [t, setT] = useTiling()
  const [r, setR] = useRenderer()
  // A native <details> only closes on its own summary; close it on any click
  // outside too, like a menu.
  const ref = useRef<HTMLDetailsElement | null>(null)
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      const el = ref.current
      if (el?.open && !el.contains(e.target as Node)) el.open = false
    }
    document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [])
  return (
    <details className="map-settings" ref={ref}>
      <summary title="Map display settings" aria-label="Map display settings">⚙</summary>
      {/* Every pointer phase stops here: the menu floats over the map, and a
          tap on it must not reach the cell underneath (on a phone a tap on
          the renderer toggle also drilled the map — the synthesized mouse
          sequence, not the click, is what the cells saw). */}
      <div className="menu" onClick={e => e.stopPropagation()} onPointerDown={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} onTouchStart={e => e.stopPropagation()} onTouchEnd={e => e.stopPropagation()}>
        <Explain text={TILING_TIP}>
          <label className="row">
            <input type="checkbox" checked={t === 'gaps'} onChange={e => setT(e.target.checked ? 'gaps' : 'shared')} />
            gaps between cells
          </label>
        </Explain>
        <Explain text={RENDERER_TIP}>
          <span className="row">
            renderer
            <span className="seg">
              {(['dom', 'canvas'] as const).map(k => (
                <button key={k} type="button" className={r === k ? 'on' : ''} onClick={() => setR(k)}>{k === 'dom' ? 'DOM' : 'canvas'}</button>
              ))}
            </span>
          </span>
        </Explain>
      </div>
    </details>
  )
}
