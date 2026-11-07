/** hyparquet decodes only Snappy natively; the engine and overlay write zstd
 * layer-2 listings and index parquet (spec `listing-slim.md`), so every read
 * passes this. `fzstd` is pure JS; `zstdUse` swaps in zstd's own decoder as a
 * statically imported wasm module (`vendor/zstd.wasm`, `ZSTD_WASM = "1"`; Workers
 * refuse only a runtime compile). Old Snappy files keep decoding through
 * hyparquet's built-in path.
 *
 * The module is `zstddec` 0.3.4's decoder (zstd 1.5.7, decoder only, no JS glue;
 * sha256 8b093c4c144273776d511f3baaa0fec32c111500c2798cb4569a71479020e72a) with
 * one edit, its memory section's initial size, 258 pages to 64; `zstd.test.ts`
 * pins the result. An isolate is charged a module's whole initial memory, touched
 * or not (measured against the kill threshold: spec `latency-observability.md`).
 * Each decode is synchronous and frees all it took before returning, so concurrent
 * reads share one heap one at a time, and what it needs is one frame's, never a
 * sum. Linear memory never shrinks, so it must not grow either: a frame that could
 * need more than `WASM_INITIAL_BYTES` stays on fzstd. */
import { decompress } from 'fzstd'
import type { Compressors } from 'hyparquet'

/** The module's initial linear memory (64 pages). */
export const WASM_INITIAL_BYTES = 64 * 65536
/** Input + output past this stays on fzstd, leaving the rest for the module's own data, stack and decoding context
 *  (under 200 KiB together), so the heap never grows, whatever the frames. */
export const WASM_FRAME_MAX = WASM_INITIAL_BYTES * 7 / 8

type ZstdExports = {
  memory: WebAssembly.Memory
  malloc: (n: number) => number
  free: (p: number) => void
  ZSTD_decompress: (dst: number, dstCapacity: number, src: number, srcSize: number) => number
}

let wanted: WebAssembly.Module | null = null
let inst: ZstdExports | null = null
/** Set once this isolate's module failed: fzstd for the rest of its life. */
let broken = false

function giveUp(what: string, e: unknown): void {
  broken = true
  inst = null
  console.log(`zstd wasm: ${what}, fzstd from here on: ${(e as Error)?.message ?? e}`)
}

function instance(): ZstdExports | null {
  if (inst || broken || !wanted) return inst
  try {
    inst = new WebAssembly.Instance(wanted, { env: { emscripten_notify_memory_growth: () => {} } }).exports as unknown as ZstdExports
  } catch (e) {
    giveUp('instantiating failed', e)
  }
  return inst
}

/** `input` decoded in the module, or null when it should be decoded by fzstd: too big, no memory, or a frame the
 *  module refuses (fzstd then decides what such a frame means, as it always has). */
function decodeWasm(x: ZstdExports, input: Uint8Array, outputLength: number): Uint8Array | null {
  const src = x.malloc(input.length)
  const dst = x.malloc(outputLength)
  if (!src || !dst) {
    x.free(src)
    x.free(dst)
    return null
  }
  new Uint8Array(x.memory.buffer).set(input, src)
  const got = x.ZSTD_decompress(dst, outputLength, src, input.length)
  const out = got === outputLength ? new Uint8Array(x.memory.buffer, dst, outputLength).slice() : null
  x.free(src)
  x.free(dst)
  return out
}

function decode(input: Uint8Array, outputLength: number): Uint8Array {
  const x = input.length + outputLength <= WASM_FRAME_MAX ? instance() : null
  if (x) {
    try {
      const out = decodeWasm(x, input, outputLength)
      if (out) return out
    } catch (e) {
      giveUp('decoding trapped', e)
    }
  }
  return decompress(input, new Uint8Array(outputLength))
}

/** Which decoder this isolate's reads use: the module, or null for fzstd. `_middleware.ts` calls it per request from
 *  the deployment's `ZSTD_WASM`; repeating the same answer does nothing. */
export function zstdUse(module: WebAssembly.Module | null): void {
  if (module === wanted) return
  wanted = module
  inst = null
  broken = false
}

export const compressors: Compressors = { ZSTD: decode }
