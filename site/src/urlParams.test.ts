import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { URL_PARAMS } from './urlParams'

/** Every `.ts`/`.tsx` source under `dir` but tests. */
const sources = (dir: string): string[] => readdirSync(dir).flatMap(f => {
  const p = join(dir, f)
  return statSync(p).isDirectory() ? sources(p) : /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f) ? [p] : []
})

/** The keys the site's use-prms hooks read by a literal: `useUrlState('k'`, `useMultiUrlState('k'`, `useUrlStates({ k: …`, `useUrlAlias({ keys: [...] `. */
function hookKeys(): string[] {
  const keys = new Set<string>()
  for (const f of sources(__dirname)) {
    const s = readFileSync(f, 'utf8')
    for (const m of s.matchAll(/use(?:Multi)?UrlState\(\s*['"]([\w-]+)['"]/g)) keys.add(m[1])
    for (const m of s.matchAll(/useUrlStates\(\{([^}]*)\}/g)) for (const k of m[1].matchAll(/(\w+)\s*:/g)) keys.add(k[1])
    for (const m of s.matchAll(/useUrlAlias[^(]*\(\{[\s\S]*?keys:\s*\[([^\]]*)\]/g)) for (const k of m[1].matchAll(/['"]([\w-]+)['"]/g)) keys.add(k[1])
  }
  return [...keys].sort()
}

describe('URL_PARAMS', () => {
  it('describes every key a hook reads', () => {
    const keys = hookKeys()
    expect(keys.length).toBeGreaterThan(40)
    expect(keys.filter(k => !(k in URL_PARAMS))).toEqual([])
  })
  it('gives each a label and a description, and pages only as routes', () => {
    expect(Object.entries(URL_PARAMS).filter(([, d]) => !d.label || !d.description || d.pages?.some(p => !p.startsWith('/'))).map(([k]) => k)).toEqual([])
  })
})
