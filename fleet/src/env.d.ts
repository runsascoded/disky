declare module 'virtual:fleet-docs' {
  /** The docs pages (`vite.config.ts` `docPages`), index first: its URL slug (`''` = `/`), repo-relative path, text. */
  export const pages: { slug: string; path: string; md: string }[]
  /** Where relative links in them point: the repo's URL and ref. */
  export const repo: string
  export const ref: string
}
