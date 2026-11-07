import { defineConfig } from 'vitest/config'
import { writeBuildId } from './scripts/build-id.mjs'

// The Functions import the build's identity (`functions/_lib/build.ts`): generated, untracked.
writeBuildId()

// Unit specs for the pure server folds (`functions/_lib`), client helpers and
// the render bench's fold (`bench/lib.ts`); the Playwright suite under e2e/
// has its own runner (`pnpm test:e2e`), as does the bench (`pnpm bench`). The fixture-wide sweeps (interval store,
// static anchors/drill) take seconds alone and blow vitest's 5 s default when the whole suite runs in parallel.
export default defineConfig({
  // A Function's statically imported wasm is a compiled module on Workers (wrangler's bundling): the same under test.
  plugins: [{
    name: 'zstd-wasm-module',
    enforce: 'pre',
    load: id => id.endsWith('/vendor/zstd.wasm') ? `import { readFileSync } from 'node:fs'\nexport default new WebAssembly.Module(readFileSync(${JSON.stringify(id)}))\n` : null,
  }],
  test: { include: ['functions/**/*.test.ts', 'src/**/*.test.ts', 'bench/**/*.test.ts'], testTimeout: 30_000 },
})
