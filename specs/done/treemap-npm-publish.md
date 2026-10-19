# Publish `@open-athena/treemap` to npm (before Mon 10/19)

From the disky deep-dive deck (`~/c/oa/disky-deep-dive`), 2026-10-18. Ryan OK'd publishing ("tell US to do it"). The talk's treemap slide demos the package live, and would rather say `pnpm add @open-athena/treemap` than "pin a dist-branch SHA".

## Status (2026-10-18, controller)

Steps 1–3 and 5 done; **step 4 (publish) waits on Ryan's explicit go** (publishing is public and permanent).
- `tsup.config.ts`: ESM + `.d.ts` + sourcemaps per subpath (`index`, `squarify`, `parseColor`, `divergingColor`, `svg`, `voronoi/index`), peers external, JSX automatic, `styles.css` copied to `dist/`. `build` and `prepack` run it; `typecheck` stays `tsc -b --noEmit`.
- `publishConfig` points `main`/`module`/`types`/`exports` at `dist/` (`./styles.css` included); `files: ["dist", "README.md"]`; the workspace's `main`/`exports` stay on `src/`.
- Checked: the packed tarball's `package.json` points at `dist/`, no `.tsx`, 720 KB; a scratch Vite 7 + React 19 app (`tmp/treemap-consumer`, its own workspace) installs the tarball, typechecks with `skipLibCheck: false`, builds, and renders the README's `<Treemap>` in Chrome with no JSX config and no errors. Package tests (191), typecheck, the site build and `@disk-tree/react`'s tests pass.
- README: `pnpm add @open-athena/treemap` first, the dist-branch SHA pin as the alternative. `build-dist.yml` now also fires on `cloud` (it only fired on `main`, why the last dist is 10/14) and its dist branch carries the same compiled build.

Renamed `@rdub/treemap` → `@open-athena/treemap` (Ryan, 2026-10-18). `site` keeps an alias `"@rdub/treemap": "workspace:@open-athena/treemap@*"` for `@rdub/file-tree`'s peer dependency until file-tree switches. To publish: `pnpm -C packages/treemap publish --access public`, then `git tag treemap-v0.1.0`. Trusted publishing (GitHub Actions OIDC, no token) can be set up on npmjs.com only once the package exists.

## State (as found)

- `packages/treemap` is `0.1.0`. `main` / `types` / `exports` point at `./src/*.ts(x)`, and `build` is only `tsc --noEmit`.
- It ships on the npm-dist `dist/treemap` branch, which is the same raw TS/TSX source. The newest dist is `ed136b03` (from `5b6a9b7`, 10/14).
- `build-dist.yml` triggers on pushes to `main`, but the working branch is `cloud`. Is that why the last dist is from 10/14? Check.
- **Consumers have to compile the TSX themselves.** The deck hit this. Vite's dep pre-bundling (rolldown) left JSX raw in `.vite/deps/@open-athena_treemap.js`, and the default oxc pass did the same for the raw files. The deck needed a custom `transformWithOxc` pre plugin plus `optimizeDeps.exclude` (see the deck's `vite.config.ts`). A published package shouldn't need that.
- npm: `npm whoami` is `rdub` (owner of the `@rdub` scope and the `open-athena` org); the name is `@open-athena/treemap` (Ryan, 2026-10-18: OA's npm org, matching GitHub `Open-Athena` and the PyPI org).

## Ask

1. **Compiled build** with tsup, matching `~/c/js/use-kbd/tsup.config.ts`:
   - entries: `src/index.ts`, `squarify`, `parseColor`, `divergingColor`, `svg`, `voronoi/index`;
   - ESM (CJS optional), `dts: true`, sourcemaps;
   - `external: ['react', 'react-dom', 'd3-hierarchy', 'd3-voronoi-treemap']`;
   - JSX runtime automatic;
   - `styles.css` copied to `dist/`.
2. **Keep the workspace on source.** `site/` and `packages/react` use `workspace:*` and benefit from importing `src` directly (HMR, no build step). So leave `main` / `exports` on `src`, and put the dist entry points in `publishConfig` (`main`, `module`, `types`, `exports` per subpath, including `./styles.css`). pnpm rewrites those at `pnpm publish` / `pack` time. Set `files: ["dist", "README.md"]` for the published tarball.
3. **Check before publishing:**
   - `pnpm -C packages/treemap pack`, then confirm the tarball's `package.json` points at `dist/` and contains no `.tsx`.
   - Install the tarball into a scratch Vite + React app under `tmp/` and render a `<Treemap>`, with **no** custom JSX config. That's the case that broke.
   - `pnpm -C packages/treemap test`.
4. **Publish** `0.1.0` with `pnpm publish --access public` from `packages/treemap`, then tag `treemap-v0.1.0`.
5. **README:** replace the "no npm publish (yet)" install section with `pnpm add @open-athena/treemap`, keeping the dist-branch SHA pin as an alternative. Keep the `import '@open-athena/treemap/styles.css'` note.
6. `publish-treemap.yml`: publishes a **dist commit** (Ryan: npm should be the `dist/treemap` SHA's bytes). Tag a **source** commit `treemap-v<version>` (a tag push runs the workflow from the tagged commit, and dist commits hold none); the job takes the newest dist commit built from an ancestor of the tag with `packages/treemap` unchanged since, strips `-dist.<sha>` from its version (which must match the tag), and `npm publish --ignore-scripts` over trusted publishing (OIDC, no token). A version already on npm is skipped.

**Published 2026-10-19:** `@open-athena/treemap@0.1.0`, by hand (OTP) from dist commit `f54c983e` (built from `e947ae09`); verified installing from npm into a fresh Vite + React app (typecheck, build). Trusted publisher set (repo `runsascoded/disky`, workflow `publish-treemap.yml`, `npm publish` allowed). **0.1.1 published by the workflow** (tag `treemap-v0.1.1` on `bc717d22`, dist commit `8485a722`), with provenance; its first try failed E422 until `package.json` carried `repository` (npm's provenance check matches it to the workflow's repo). The deck uses `@open-athena/treemap@^0.1.0`.

## Consumer follow-up (the deck, not this repo)

Once it's on npm, the deck switches to `@open-athena/treemap@^0.1.0` and drops its `rdub-treemap-tsx` Vite plugin and `optimizeDeps` tweaks. Tell Ryan the version; the deck session does the swap.
