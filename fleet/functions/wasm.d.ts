/** A `.wasm` import is a build-time-compiled module: Workers forbid compiling wasm from bytes at runtime, so resvg's
 *  wasm is imported this way (wrangler's bundling; `routes.test.ts` mocks it with the same under test). */
declare module '*.wasm' {
  const module: WebAssembly.Module
  export default module
}
