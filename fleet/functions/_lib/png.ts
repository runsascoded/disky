/** A card's SVG → PNG (`@disk-tree/pages-kit/og/render`, as `site/`'s cards), with the Inter faces the build copies to
 *  `FONT_FILES` (`vite.config.ts`). */
import RESVG from '@resvg/resvg-wasm/index_bg.wasm'
import { ensureWasm, FONT_FILES, svgToPng } from '@disk-tree/pages-kit/og/render'
import { ZOOM } from './card.js'
import type { Env } from './env.js'

let fontsP: Promise<Uint8Array[]> | null = null

/** The card fonts, fetched from the app's own assets once per isolate (a failure is retried on the next call). */
export function fonts(env: Env, origin: string): Promise<Uint8Array[]> {
  if (!fontsP) {
    fontsP = Promise.all(FONT_FILES.map(async f => {
      const r = await env.ASSETS.fetch(new Request(origin + f))
      if (!r.ok) throw new Error(`font ${f}: ${r.status}`)
      return new Uint8Array(await r.arrayBuffer())
    }))
    fontsP.catch(() => { fontsP = null })
  }
  return fontsP
}

export async function renderPng(env: Env, origin: string, svg: string): Promise<Uint8Array> {
  await ensureWasm(RESVG)
  return svgToPng(svg, await fonts(env, origin), ZOOM)
}
