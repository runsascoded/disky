import { defineConfig } from 'vitest/config'

// Unit specs for the pure server folds (`functions/_lib`), client helpers and
// the render bench's fold (`bench/lib.ts`); the Playwright suite under e2e/
// has its own runner (`pnpm test:e2e`), as does the bench (`pnpm bench`). The fixture-wide sweeps (interval store,
// static anchors/drill) take seconds alone and blow vitest's 5 s default when the whole suite runs in parallel.
export default defineConfig({
  test: { include: ['functions/**/*.test.ts', 'src/**/*.test.ts', 'bench/**/*.test.ts'], testTimeout: 30_000 },
})
