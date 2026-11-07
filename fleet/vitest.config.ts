import { defineConfig } from 'vitest/config'

// The Functions' specs, and the page's pure helpers. A Function's statically imported wasm (resvg's, from
// `node_modules`) is a compiled module on Workers (wrangler's bundling); a spec that renders mocks the import with the
// same compiled module (`routes.test.ts`), so the card is rasterized for real.
export default defineConfig({
  test: { include: ['functions/**/*.test.ts', 'src/**/*.test.ts'], testTimeout: 30_000 },
})
