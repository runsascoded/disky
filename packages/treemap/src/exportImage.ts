/**
 * Export the current treemap view as a PNG — copy to the clipboard or download.
 *
 * The canvas renderer already draws the whole map (cells + labels) to one
 * `<canvas>` at `devicePixelRatio`, so the pixels a teammate would screenshot
 * already exist: we read them straight off that canvas — no DOM-to-image
 * dependency, crisp on retina without re-rendering. `composeExport` optionally
 * composites a **title line** (the crumb text — root path + total) above the
 * map on an offscreen canvas in the page's current theme colours; the base
 * export is the map alone (what the teammate clipped).
 *
 * Consumed by `<Treemap exportable>` (spec `treemap-export-image.md`); the
 * helpers are standalone so a consumer can wire its own affordance.
 */

/** Whether an export was copied to the clipboard or saved as a file. */
export type ExportKind = 'copy' | 'download'

/** Context handed to `ExportOptions.filename`. */
export interface ExportContext<T> {
  /** The current view's root node (crumb tail). */
  node: T
  /** Full ancestry from the tree root to `node`. */
  path: T[]
}

/** `Treemap`'s `exportable` prop, spelled out. `true` ≡ `{}` (map only). */
export interface ExportOptions<T = unknown> {
  /** Output filename (a `.png` is appended when missing). Default:
   *  `<view-basename>-<YYYYMMDD-HHMM>.png`. */
  filename?: (ctx: ExportContext<T>) => string
  /** Composite the crumb text (root path + total) as a title line above the
   *  map. Default: false (the bare map, what the teammate clipped). */
  title?: boolean
}

/** Theme colours + scale for `composeExport`, read off the live map element. */
export interface ComposeOptions {
  /** Title line, or null for the bare map (returns the source unchanged). */
  title: string | null
  /** Title bar background (blended with the map ground). */
  bg: string
  /** Title text colour (the map's inherited ink). */
  ink: string
  /** Device pixel ratio the source canvas was drawn at. */
  dpr: number
}

const TITLE_PAD = 10
const TITLE_LINE_H = 22
const TITLE_FONT = '600 14px system-ui, -apple-system, sans-serif'

/** Truncate `text` with an ellipsis to fit `maxW` px in the current ctx font. */
function fit(ctx: CanvasRenderingContext2D, text: string, maxW: number): string {
  if (maxW <= 0) return ''
  if (ctx.measureText(text).width <= maxW) return text
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (ctx.measureText(text.slice(0, mid) + '…').width <= maxW) lo = mid
    else hi = mid - 1
  }
  return lo > 0 ? text.slice(0, lo) + '…' : ''
}

/**
 * The export canvas for `source`: the map alone when `title` is null, else a
 * new canvas with the title line composited above it (theme colours, a small
 * margin). The source is drawn 1:1 — it's already at `dpr`.
 */
export function composeExport(source: HTMLCanvasElement, o: ComposeOptions): HTMLCanvasElement {
  if (!o.title) return source
  const { dpr } = o
  const cssW = source.width / dpr
  const barH = TITLE_LINE_H + TITLE_PAD
  const out = document.createElement('canvas')
  out.width = source.width
  out.height = source.height + Math.round(barH * dpr)
  const ctx = out.getContext('2d')
  if (!ctx) return source
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  // Ground the whole export (title bar + behind the map's gutters) in the bg.
  ctx.fillStyle = o.bg
  ctx.fillRect(0, 0, cssW, barH + source.height / dpr)
  // Title text, clipped to the width.
  ctx.fillStyle = o.ink
  ctx.font = TITLE_FONT
  ctx.textBaseline = 'middle'
  ctx.fillText(fit(ctx, o.title, cssW - 2 * TITLE_PAD), TITLE_PAD, barH / 2)
  // The map below the bar (device px → css → device is 1:1, so crisp).
  ctx.drawImage(source, 0, barH, cssW, source.height / dpr)
  return out
}

/** A canvas → PNG `Blob` (rejects if the browser can't encode one). */
export function canvasToPngBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(b => (b ? resolve(b) : reject(new Error('canvas.toBlob returned null'))), 'image/png')
  })
}

/**
 * Copy `blob` to the clipboard as `image/png`. Returns false (rather than
 * throwing) when `ClipboardItem` / `clipboard.write` is unavailable — Firefox
 * behind a flag, or a non-secure context — so the caller can fall back to a
 * download. Needs a user gesture + secure context, both of which hold for a
 * button click on an HTTPS page.
 */
export async function copyPng(blob: Blob): Promise<boolean> {
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) return false
  try {
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    return true
  } catch {
    return false
  }
}

/** Save `blob` as `filename` via a transient object-URL `<a download>`. */
export function downloadPng(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename.endsWith('.png') ? filename : `${filename}.png`
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoke after the click has a chance to start the download.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** `<view-basename>-<YYYYMMDD-HHMM>.png` — the default export filename. */
export function defaultExportFilename(label: string, when: Date = new Date()): string {
  const p2 = (n: number) => String(n).padStart(2, '0')
  const stamp =
    `${when.getFullYear()}${p2(when.getMonth() + 1)}${p2(when.getDate())}` +
    `-${p2(when.getHours())}${p2(when.getMinutes())}`
  // Filename-safe basename: drop a trailing slash, keep the last segment, map
  // anything not [A-Za-z0-9._-] to '-'.
  const base = (label.replace(/\/+$/, '').split('/').pop() || 'treemap').replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '')
  return `${base || 'treemap'}-${stamp}.png`
}
