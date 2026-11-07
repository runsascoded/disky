import { copyFileSync } from 'node:fs'
import { defineConfig } from 'tsup'

// The published build (specs/treemap-npm-publish.md): compiled ESM + types per subpath, so a consumer needs no TSX
// handling. The workspace (`site/`, `packages/react`) keeps importing `src/` (`main`/`exports`); `publishConfig`
// points the tarball at `dist/`.
export default defineConfig({
  entry: {
    index: 'src/index.ts',
    squarify: 'src/squarify.ts',
    parseColor: 'src/parseColor.ts',
    divergingColor: 'src/divergingColor.ts',
    svg: 'src/svg.ts',
    'voronoi/index': 'src/voronoi/index.ts',
  },
  format: ['esm'],
  dts: true,
  sourcemap: true,
  clean: true,
  treeshake: true,
  splitting: true,
  external: ['react', 'react-dom', 'd3-hierarchy', 'd3-voronoi-treemap'],
  esbuildOptions: o => { o.jsx = 'automatic' },
  onSuccess: async () => { copyFileSync('src/styles.css', 'dist/styles.css') },
})
