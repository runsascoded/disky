/** The paths `public/_routes.json` excludes from the Functions (served as static assets, never by the catch-all,
 *  whose SPA fallback answers any other path with `index.html`). Mirrored here so the Functions can check a config
 *  against it (`functions/routes.test.ts` pins the two equal). */
export const FUNCTIONS_EXCLUDED: readonly string[] = ['/_fonts/*', '/icons/*', '/og.jpg', '/gcs.png', '/favicon.svg', '/llms.txt']

/** Whether a site path is excluded from the Functions: an exact entry, or under a `/<dir>/*` one. */
export const excludedFromFunctions = (path: string, excluded: readonly string[] = FUNCTIONS_EXCLUDED): boolean =>
  excluded.some(e => (e.endsWith('/*') ? path.startsWith(e.slice(0, -1)) : path === e))
