/// <reference types="node" />
/** Every response-cache (Cache API) and KV key in `functions/`, by kind, and whether it carries the deployment
 *  environment's tag (`edgeCache.ts` `cacheEnvTag`): a preview host's (a PR's `pr-N`) answers must never reach the
 *  dev stack's keys, nor dev's a preview's. A new `https://<kind>.cache/` key fails this until it is listed here. */
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const ROOT = join(fileURLToPath(import.meta.url), '../..')
const sources = (dir: string): string[] => readdirSync(dir, { withFileTypes: true }).flatMap(e =>
  e.isDirectory() ? sources(join(dir, e.name)) : e.name.endsWith('.ts') && !e.name.endsWith('.test.ts') ? [join(dir, e.name)] : [])

/** `file: kind` for each synthetic cache host built in source, `cacheKeyFor`'s template included. */
const kinds = () => sources(ROOT).flatMap(f => [...readFileSync(f, 'utf8').matchAll(/https:\/\/([a-z${}-]+)\.cache\//g)]
  .map(m => `${relative(ROOT, f)}: ${m[1]}`)).sort()

describe('cache keys', () => {
  it('each computed answer\'s key is tagged by `cacheEnvTag`; the rest are immutable store bytes or per-origin, per-build', () => {
    expect(kinds()).toEqual([
      // Immutable store objects and versioned parses of them (`coloKey`: `&d=<doc version>`), the same bytes on every host.
      '_lib/edgeCache.ts: ${kind}',          // `cacheKeyFor`: every `cacheEnvTag` key (colo + KV)
      '_lib/index.ts: index-blob',
      '_lib/index.ts: index-footer',
      '_lib/union.ts: union-observations',   // tagged: `~<cacheEnvTag>/`
      'api/health.ts: health',               // tagged: its first segment
    ])
  })

  it('every `cacheKeyFor` caller passes `cacheEnvTag`', () => {
    const calls = sources(ROOT).flatMap(f => [...readFileSync(f, 'utf8').matchAll(/cacheKeyFor\('([a-z-]+)', ([^,]+),/g)]
      .map(m => [relative(ROOT, f), m[1], m[2].startsWith('cacheEnvTag(') || m[2] === 'tag' ? 'tagged' : m[2]]))
    expect(calls.filter(([, , how]) => how !== 'tagged')).toEqual([])
    expect(calls.map(([f, k]) => `${f}: ${k}`).sort()).toEqual([
      'api/diff.ts: diff',
      'api/diff/level.ts: level',
      'api/dups.ts: dups',
      'api/filter-cover.ts: filter-cover',
      'api/largest.ts: largest',
      'api/name-matches.ts: name-matches',
      'api/series.ts: series',
      'api/subtree.ts: subtree',
    ])
  })
})
