/** Bindings the Pages Functions see (`wrangler.toml`). */
export interface Env {
  /** R2 bucket holding reduced scans: `<prefix><uuid>.parquet` + `.scan.json`. */
  SCANS: R2Bucket
  /** Key prefix within the bucket (with trailing slash). */
  SCANS_PREFIX?: string
  /** Grants + access log for the gate (spec `specs/done/pages-auth.md`). */
  DB?: D1Database
  /** HMAC key for session cookies (Pages secret). Localhost gets a fixed dev value. */
  SESSION_SECRET?: string
  /** Comma/space-separated emails that SSO admits — they get `view` + `admin` (Pages secret). */
  ALLOWED_EMAILS?: string
  /** `https://<team>.cloudflareaccess.com`: the Zero Trust org whose JWTs `/auth/sso` accepts. */
  ACCESS_TEAM_DOMAIN?: string
  /** The Access application's AUD tag. */
  ACCESS_AUD?: string
  /** When set (any non-empty value), this is a public **open** demo: no auth
   *  gate, and `/api/capabilities` reports `auth: false`. A corpus of public
   *  data only — never point an open deployment at a private (e.g. laptop)
   *  scan. Needs no `DB`/`SESSION_SECRET`. */
  PUBLIC_OPEN?: string
  /** Max objects the edge R2 CFN deletes inline before deferring the run to the
   *  drainer (spec `staged-delete.md` CP7). Default 1000. */
  DELETE_THRESHOLD?: string
  /** Per-bucket R2 bindings for the CFN executor: `R2_<bucket>` (bucket name
   *  sanitized to an identifier), e.g. `R2_ctbk` -> the `ctbk` bucket. Bound in
   *  `wrangler.toml`; absent buckets fall back to the drainer. */
  [binding: `R2_${string}`]: R2Bucket | undefined
}

export const scansPrefix = (env: Env): string => env.SCANS_PREFIX ?? ''

/** Public open demo (see `PUBLIC_OPEN`): the gate is off and every `/api/*`
 *  route is served without a session. */
export const isOpen = (env: Env): boolean => !!env.PUBLIC_OPEN
