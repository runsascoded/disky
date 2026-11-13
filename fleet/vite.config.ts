/**
 * The fleet app's build (specs/disky-oa-dev.md). Build-time env, all optional:
 * - `FLEET_ORIGIN`: the site's origin, for absolute `og:` URLs (`%ORIGIN%` in the HTML); unset = relative.
 * - `FLEET_DOCS_INDEX`: the Markdown file `/` renders, repo-relative (default `fleet/docs/index.md`; a deployment
 *   branch points it at its own README index).
 * - `FLEET_REPO`, `FLEET_REF`: where the docs' relative links point (default this repo's `cloud` on GitHub).
 *
 * The docs are read from disk here into the virtual module `virtual:fleet-docs` (`src/docs.ts`): the index, the
 * project README (`/docs/disky`), and every other `fleet/docs/*.md` and `docs/**\/*.md` (`/docs/<name>`). The card
 * fonts are `site/`'s (`public/_fonts/Inter-{400,600}.ttf`), served and emitted at the same paths.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..')
const FONTS = ['Inter-400.ttf', 'Inter-600.ttf']
const FONT_DIR = resolve(REPO, 'site/public/_fonts')

export interface DocPage { slug: string; path: string; md: string }

const mdFiles = (dir: string): string[] =>
  !existsSync(dir) ? [] : readdirSync(dir).flatMap(f => {
    const p = join(dir, f)
    return statSync(p).isDirectory() ? mdFiles(p) : f.endsWith('.md') ? [p] : []
  }).sort()

/** The docs pages, index first. */
export function docPages(): DocPage[] {
  const index = resolve(REPO, process.env.FLEET_DOCS_INDEX ?? 'fleet/docs/index.md')
  const page = (slug: string, p: string): DocPage => ({ slug, path: relative(REPO, p), md: readFileSync(p, 'utf8') })
  const rest = [...mdFiles(resolve(HERE, 'docs')), ...mdFiles(resolve(REPO, 'docs'))].filter(p => p !== index)
  const slugOf = (p: string) => relative(p.startsWith(resolve(HERE, 'docs')) ? resolve(HERE, 'docs') : resolve(REPO, 'docs'), p).replace(/\.md$/, '').toLowerCase()
  return [page('', index), page('disky', resolve(REPO, 'README.md')), ...rest.map(p => page(slugOf(p), p))]
}

function docs(): Plugin {
  const id = 'virtual:fleet-docs'
  return {
    name: 'fleet-docs',
    resolveId: s => (s === id ? `\0${id}` : null),
    load(s) {
      if (s !== `\0${id}`) return null
      const pages = docPages()
      for (const p of pages) this.addWatchFile(resolve(REPO, p.path))
      const repo = (process.env.FLEET_REPO ?? 'https://github.com/Open-Athena/disky').replace(/\/+$/, '')
      return `export const pages = ${JSON.stringify(pages)}\nexport const repo = ${JSON.stringify(repo)}\nexport const ref = ${JSON.stringify(process.env.FLEET_REF ?? 'cloud')}\n`
    },
  }
}

function fonts(): Plugin {
  return {
    name: 'fleet-fonts',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const f = FONTS.find(n => req.url === `/_fonts/${n}`)
        if (!f) return next()
        res.setHeader('content-type', 'font/ttf')
        res.end(readFileSync(join(FONT_DIR, f)))
      })
    },
    generateBundle() {
      for (const f of FONTS) this.emitFile({ type: 'asset', fileName: `_fonts/${f}`, source: readFileSync(join(FONT_DIR, f)) })
    },
  }
}

function origin(): Plugin {
  return { name: 'fleet-origin', transformIndexHtml: html => html.replaceAll('%ORIGIN%', (process.env.FLEET_ORIGIN ?? '').replace(/\/+$/, '')) }
}

export default defineConfig({
  plugins: [react(), docs(), fonts(), origin()],
  build: { rollupOptions: { input: { index: resolve(HERE, 'index.html'), fleet: resolve(HERE, 'fleet.html') } } },
  server: {
    port: 3870,
    host: true,
    allowedHosts: true,
    // the Functions under `wrangler pages dev` (`pnpm dev:pages`)
    proxy: { '/api': 'http://localhost:3871', '/og': 'http://localhost:3871', '/auth': 'http://localhost:3871' },
  },
})
