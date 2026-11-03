import { defineConfig } from 'vitest/config'

export default defineConfig({ test: { setupFiles: ['./src/fixtures/worker-streams.cjs'] } })
