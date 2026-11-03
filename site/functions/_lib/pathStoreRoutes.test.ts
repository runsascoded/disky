// Every Function that reads the path index goes through `withPathStore` (gcs's `specs/d1-footer-writes.md` audit):
// the interval store for the dates it holds, never per-scan `index_row_groups` rows, which a deployment running
// `index-sync -R` no longer writes. A route importing an index reader without it is a per-scan read on an unpinned
// deployment. The exceptions say why.
import { describe, expect, it } from 'vitest'

// The functions tsconfig has no Node types (as `usage.test.ts`): `node:fs`, imported by name, typed here.
const { readdirSync, readFileSync } = (await import(/* @vite-ignore */ 'node:' + 'fs')) as {
  readdirSync(p: string, o: { recursive: true }): string[]
  readFileSync(p: string, enc: 'utf8'): string
}
const ROOT = new URL('..', (import.meta as unknown as { url: string }).url).pathname
const READERS = /from ['"](?:\.\.\/)+_lib\/(?:view|ownerTotals|prefixes|stagedSlack|overTime|activitySize)\.js['"]|\bopenIndex\b/

/** Routes that read the index without `withPathStore`, on purpose. */
const EXCEPTIONS: Record<string, string> = {
  'api/age-pyramid.ts': 'age pyramids: the age store first (`readAgePoint`), else their own per-scan variants, which no interval store holds',
}

/** The Functions' route files (relative to `functions/`): every `.ts` but `_lib/` and tests. */
const routes = (): string[] =>
  readdirSync(ROOT, { recursive: true }).filter(r => r.endsWith('.ts') && !r.endsWith('.test.ts') && !r.startsWith('_lib/')).sort()

describe('index readers', () => {
  it('every route reading the path index wraps its env with `withPathStore`, or is a listed exception', () => {
    const readers = routes().filter(r => READERS.test(readFileSync(ROOT + r, 'utf8')))
    // `matchQuery` (`_lib/matchQuery.ts`, the name-filter routes' front half) wraps with it
    const missing = readers.filter(r => !/\bwithPathStore\b|\bmatchQuery\b/.test(readFileSync(ROOT + r, 'utf8')))
    expect(missing).toEqual(Object.keys(EXCEPTIONS).sort())
  })
})
