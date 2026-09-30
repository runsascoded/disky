/** hyparquet decodes only Snappy natively; the engine and overlay write zstd
 * layer-2 listings and index parquet (spec `listing-slim.md`), so every read
 * passes this. `fzstd` is pure JS: no wasm, which Workers can't compile at
 * runtime. Old Snappy files keep decoding through hyparquet's built-in path. */
import { decompress } from 'fzstd'
import type { Compressors } from 'hyparquet'

export const compressors: Compressors = {
  ZSTD: (input: Uint8Array, outputLength: number) => decompress(input, new Uint8Array(outputLength)),
}
