/** A `.wasm` import resolves to a build-time-compiled module. Workers forbid
 *  compiling wasm from bytes at runtime (`WebAssembly.instantiate` on a buffer
 *  is "disallowed by embedder"), so the resvg wasm must be imported this way —
 *  wrangler uploads it as a module and the import is the compiled `Module`. */
declare module '*.wasm' {
  const module: WebAssembly.Module
  export default module
}
