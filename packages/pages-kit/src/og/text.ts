/** Text and axis helpers for server-drawn SVG cards (no DOM, no canvas): escaping, byte formatting, Inter's advance
 *  widths (`interMetrics.ts`), round ticks and label spreading. Shared by `site/`'s cards and `fleet/`'s. */
export { interWidth } from './interMetrics.js'

export function esc(s: string): string {
  return s.replace(/[<>&"']/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' })[c]!)
}

/** Binary bytes, 3 significant figures: `51.1 TiB`. */
export function fmtB(n: number): string {
  return fmtUnits(n, 1024, ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'])
}

/** `fmtB` in decimal units (`15.4 TB`): a store billed in TB, as Hugging Face's digest. */
export function fmtSI(n: number): string {
  return fmtUnits(n, 1000, ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB'])
}

function fmtUnits(n: number, base: number, u: string[]): string {
  let i = 0
  let v = n
  while (v >= base && i < u.length - 1) { v /= base; i++ }
  return i === 0 ? `${v} B` : `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${u[i]}`
}

/** ~`n` round ticks over `[lo, hi]`: steps of 1, 2, 2.5 or 5 × 10^k. */
export function niceTicks(lo: number, hi: number, n = 6): number[] {
  const raw = (hi - lo) / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw)!
  const out: number[] = []
  // `|| 0`: a tick at a lo just under zero is -0, which would label `-0`
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 1e6) / 1e6 || 0)
  return out
}

/** Labels' y's pushed apart to at least `gap`, kept inside `[lo, hi]` (order preserved). */
export function spread(ys: number[], gap: number, lo: number, hi: number): number[] {
  const order = ys.map((y, i) => [y, i] as const).sort((a, b) => a[0] - b[0])
  const out = order.map(([y]) => y)
  for (let k = 1; k < out.length; k++) out[k] = Math.max(out[k], out[k - 1] + gap)
  const over = out.length ? out[out.length - 1] - hi : 0
  if (over > 0) for (let k = out.length - 1; k >= 0; k--) out[k] = Math.min(out[k] - over, k < out.length - 1 ? out[k + 1] - gap : Infinity)
  for (let k = 0; k < out.length; k++) out[k] = Math.max(out[k], lo + k * gap)
  const res = new Array<number>(ys.length)
  order.forEach(([, i], k) => { res[i] = out[k] })
  return res
}
