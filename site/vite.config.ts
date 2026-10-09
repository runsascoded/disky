import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig, type Plugin } from 'vite'

// Ports: `devPort` in package.json (vite), `PORT` env overrides (a second dev
// stack, or when another worktree already holds 3263); wrangler pages dev (the
// Functions) is always the next port up — `./dev` derives it the same way.
const PORT = Number(process.env.PORT ?? JSON.parse(readFileSync('package.json', 'utf8')).devPort)
const WRANGLER = `http://localhost:${PORT + 1}`
// `API_ORIGIN=https://r2.rbw.sh pnpm dev`: proxy the data + API paths to a
// DEPLOYED site instead of a local wrangler — a read-only preview of UI
// changes over real data, with no D1 seed or store creds on this machine.
// Sign-in stays local (its callback origin must be this host).
const API = process.env.API_ORIGIN ?? WRANGLER
// `API_TOKEN` (a site bearer token) authenticates that proxy, for a deployed
// site behind sign-in; the localhost page itself stays signed out.
const apiProxy = API === WRANGLER ? API : {
  target: API, changeOrigin: true,
  ...(process.env.API_TOKEN ? { headers: { Authorization: `Bearer ${process.env.API_TOKEN}` } } : {}),
}

// dev only: serve a locally-generated `tmp/series.json` (from `dt-cloud series
// -r http://localhost:3254/data -o tmp/series.json`) at /data/series.json, so
// the scoped size chart can be previewed before the index is published to the
// bucket. Registered in the plugin body so it pre-empts the /data proxy; a no-op
// (falls through to the bucket) when the file is absent.
const devSeriesIndex = {
  name: 'dev-series-index',
  configureServer(server: { middlewares: { use: (path: string, fn: (req: unknown, res: { setHeader: (k: string, v: string) => void; end: (b: Buffer) => void }, next: () => void) => void) => void } }) {
    server.middlewares.use('/data/series.json', (_req, res, next) => {
      const p = 'tmp/series.json'
      if (existsSync(p)) { res.setHeader('content-type', 'application/json'); res.end(readFileSync(p)) }
      else next()
    })
  },
}

// The deployment's guide for agents — `API.md` at the repo root, on a branch
// that carries one (gcs) — ships as `/llms.txt`: a static asset, outside the
// sign-in gate (`public/_routes.json` keeps it off the Functions). No
// `API.md` (cw-s3, the r2 demo) → no `/llms.txt`, and the SPA fallback answers.
const LLMS_SRC = fileURLToPath(new URL('../API.md', import.meta.url))
const llmsTxt: Plugin = {
  name: 'llms-txt',
  configureServer(server) {
    server.middlewares.use('/llms.txt', (_req, res, next) => {
      if (!existsSync(LLMS_SRC)) return next()
      res.setHeader('content-type', 'text/plain; charset=utf-8')
      res.end(readFileSync(LLMS_SRC))
    })
  },
  generateBundle() {
    if (existsSync(LLMS_SRC)) this.emitFile({ type: 'asset', fileName: 'llms.txt', source: readFileSync(LLMS_SRC, 'utf8') })
  },
}

// Deployment as configuration: the store this build serves (`src/stores.ts`
// registry key) and the client's auth mode (`src/auth.ts`: `app` for the
// app-session model, `public` for an open deploy) come from the same file
// wrangler reads — `STORE` / `AUTH_MODE` under
// `[vars]` in wrangler.toml — so a deployment branch declares itself in one
// place. A Pages environment's overrides (`[env.<name>.vars]`, e.g. the
// `preview` block the dev stack deploys with) apply on top when
// `CLOUDFLARE_ENV=<name>` is set — the same variable wrangler itself reads —
// so a preview build carries the preview's mode, not production's.
// `VITE_STORE` / `VITE_AUTH_MODE` in the environment still override (a CI
// build of another store). Neither set → the registry's first store, `app`.
//
// Which file: `$WRANGLER_CONFIG` if set (e.g. `wrangler.r2-dev.toml`), else
// the deployment branch's own `wrangler.toml`, else `wrangler.r2.toml` — the
// r2 demo's config, which is all `cloud` carries (it has no `wrangler.toml`;
// each child branch owns its own as a whole file).
function wranglerConfig(): string | undefined {
  const explicit = process.env.WRANGLER_CONFIG
  if (explicit) {
    if (!existsSync(explicit)) throw new Error(`WRANGLER_CONFIG=${explicit}: no such file`)
    return explicit
  }
  return ['wrangler.toml', 'wrangler.r2.toml'].find(f => existsSync(f))
}
function wranglerVars(env = process.env.CLOUDFLARE_ENV): Record<string, string> {
  const file = wranglerConfig()
  if (!file) return {}
  const vars: Record<string, string> = {}
  const sections = new Set(['[vars]', ...(env ? [`[env.${env}.vars]`] : [])])
  let inVars = false
  for (const raw of readFileSync(file, 'utf8').split('\n')) {
    const line = raw.replace(/#.*$/, '').trim()
    if (line.startsWith('[')) { inVars = sections.has(line); continue }
    const m = inVars ? /^([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"$/.exec(line) : null
    if (m) vars[m[1]] = m[2]
  }
  return vars
}
const VARS = wranglerVars()
const STORE = process.env.VITE_STORE ?? VARS.STORE ?? ''
const AUTH_MODE = process.env.VITE_AUTH_MODE ?? VARS.AUTH_MODE ?? 'app'
// Secondary stores (specs/multi-store.md phase 2): comma-separated registry
// keys, each mounted under its own path (`/meta`). `STORES_EXTRA` beside
// `STORE` in wrangler.toml, or `VITE_STORES_EXTRA` in the environment; unset →
// the single-store build, unchanged.
const STORES_EXTRA = process.env.VITE_STORES_EXTRA ?? VARS.STORES_EXTRA ?? ''
// A file store's home dir (`Users/ryan`), shown as `~` (`HOME` under [vars]).
const HOME = process.env.VITE_HOME ?? VARS.HOME ?? ''
// The deployment's name (`ROOT_LABEL` under [vars]), e.g. for a store's tab title.
const ROOT_LABEL = process.env.VITE_ROOT_LABEL ?? VARS.ROOT_LABEL ?? ''
// The prod ↔ dev host pair `g d` toggles between (`src/hosts.ts`); a deployment
// without a dev alias leaves them unset and gets no binding.
const PROD_HOST = process.env.VITE_PROD_HOST ?? VARS.PROD_HOST ?? ''
const DEV_HOST = process.env.VITE_DEV_HOST ?? VARS.DEV_HOST ?? ''
// The source link (src/SiteKbd.tsx); unset = this repo.
const REPO_URL = process.env.VITE_REPO_URL ?? VARS.REPO_URL ?? ''

export default defineConfig({
  define: {
    'import.meta.env.VITE_STORE': JSON.stringify(STORE),
    'import.meta.env.VITE_AUTH_MODE': JSON.stringify(AUTH_MODE),
    'import.meta.env.VITE_STORES_EXTRA': JSON.stringify(STORES_EXTRA),
    'import.meta.env.VITE_HOME': JSON.stringify(HOME),
    'import.meta.env.VITE_ROOT_LABEL': JSON.stringify(ROOT_LABEL),
    'import.meta.env.VITE_PROD_HOST': JSON.stringify(PROD_HOST),
    'import.meta.env.VITE_DEV_HOST': JSON.stringify(DEV_HOST),
    'import.meta.env.VITE_REPO_URL': JSON.stringify(REPO_URL),
  },
  plugins: [react(), devSeriesIndex, llmsTxt],
  server: {
    port: PORT,
    host: true,
    allowedHosts: true,
    // dev only: forward the Pages Functions (snapshot data + scan-browser API)
    // to the local `wrangler pages dev` (the next port up, with GCS HMAC creds
    // in .dev.vars). Both /data and /v1/files now read live from the bucket.
    proxy: {
      '/data': apiProxy,
      '/v1/files': apiProxy,
      // Mark & sweep console: plans/marks/sweep/whoami Functions (D1 + Batch).
      '/api': apiProxy,
      // Sign-in Functions (`/auth/google*`, `/auth/email/*`). Keep the
      // browser's Host header (Vite's string-target default rewrites it to the
      // wrangler port): the OIDC callback + emailed links derive their origin
      // from it, so they resolve to `http://localhost:<PORT>/…` — the URI that
      // must be registered on the Google client for local sign-in to work.
      '/auth': { target: WRANGLER, changeOrigin: false },
    },
  },
  // The workspace-linked `@rdub/file-tree` calls `useLocation` etc. — force a
  // single instance of these so its hooks share the app's Router/React context
  // (else the rollup build bundles a 2nd copy → "useLocation outside <Router>").
  resolve: {
    dedupe: ['react', 'react-dom', 'react-router-dom'],
  },
  optimizeDeps: {
    exclude: ['@disk-tree/react'],
  },
})
