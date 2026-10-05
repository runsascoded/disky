/** A `.wasm` import is a build-time-compiled module: Workers forbid compiling
 * wasm from bytes at runtime, so resvg's wasm is imported this way. */
declare module '*.wasm' {
  const module: WebAssembly.Module
  export default module
}
