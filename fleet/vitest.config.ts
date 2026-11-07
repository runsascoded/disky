import { defineConfig } from 'vitest/config'

// The Functions' specs. A Function's statically imported wasm (resvg's) is a compiled module on Workers (wrangler's
// bundling): the same under test, so the card is rasterized for real.
export default defineConfig({
  plugins: [{
    name: 'wasm-module',
    enforce: 'pre',
    load: id => id.endsWith('.wasm') ? `import { readFileSync } from 'node:fs'\nexport default new WebAssembly.Module(readFileSync(${JSON.stringify(id)}))\n` : null,
  }],
  test: { include: ['functions/**/*.test.ts', 'src/**/*.test.ts'], testTimeout: 30_000 },
})
