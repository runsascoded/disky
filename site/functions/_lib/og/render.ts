/** SVG → PNG with resvg-wasm, at the card's intrinsic 1200×630. A Worker may
 * not compile wasm from bytes at runtime, so the Function imports the vendored
 * `vendor/resvg.wasm` as a build-time module and passes it to `ensureWasm`;
 * Node (tests) passes the raw bytes. Fonts are TTF buffers (no system fonts on
 * the edge): the site serves them from `public/_fonts/`. */
import { Resvg, initWasm } from '@resvg/resvg-wasm'

let wasmReady: Promise<unknown> | null = null

/** Initialize resvg once per isolate; concurrent callers share the init. */
export function ensureWasm(source: WebAssembly.Module | BufferSource): Promise<unknown> {
  if (!wasmReady) wasmReady = initWasm(source)
  return wasmReady
}

/** The faces a card uses (Inter, OFL), as the site serves them. */
export const FONT_FILES = ['/_fonts/Inter-400.ttf', '/_fonts/Inter-600.ttf']

/** `ensureWasm` must have resolved first. */
export function svgToPng(svg: string, fonts: Uint8Array[]): Uint8Array {
  const resvg = new Resvg(svg, {
    font: { fontBuffers: fonts, defaultFontFamily: 'Inter', sansSerifFamily: 'Inter', loadSystemFonts: false },
  })
  try {
    return resvg.render().asPng()
  } finally {
    resvg.free()
  }
}
