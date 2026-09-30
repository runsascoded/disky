/** Rasterize the treemap card SVG (`ogSvg.ts`) to PNG with resvg-wasm — the
 *  edge half of tier B. The Worker imports the `.wasm` module and fetches the
 *  bundled Inter faces as static assets; the CI/Node test reads both from the
 *  installed package. `initWasm` must run once per isolate before `Resvg`. */
import { Resvg, initWasm } from '@resvg/resvg-wasm'

let wasmReady: Promise<unknown> | null = null

/** Initialize resvg-wasm once. `source` is whatever `initWasm` accepts — a
 *  compiled `WebAssembly.Module` (Worker `.wasm` import), a `Response`, or the
 *  raw bytes (Node). Concurrent callers share the one init. */
export function ensureWasm(source: WebAssembly.Module | Response | Promise<Response> | BufferSource): Promise<unknown> {
  if (!wasmReady) wasmReady = initWasm(source)
  return wasmReady
}

/** Render `svg` to a PNG byte array at its intrinsic size. `fonts` are TTF/OTF
 *  buffers; the first family is the default (no system fonts on the edge).
 *  `ensureWasm` must have resolved first. */
export function svgToPng(svg: string, fonts: Uint8Array[]): Uint8Array {
  const resvg = new Resvg(svg, {
    font: { fontBuffers: fonts, defaultFontFamily: 'Inter', loadSystemFonts: false },
    background: '#16181d',
  })
  return resvg.render().asPng()
}
