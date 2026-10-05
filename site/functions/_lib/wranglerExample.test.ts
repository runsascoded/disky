// `site/wrangler.example.toml` is the contract a new deployment fills in. It
// must mention every binding, var and secret the Functions' `Env` types
// declare, and set nothing that neither the Functions nor the build read.
import { beforeAll, describe, expect, it } from 'vitest'

// Node's fs, typed locally: the Functions' tsconfig is Workers-only (as in
// `searchBench.test.ts` / `og/render.test.ts`).
interface Fs {
  readFileSync(p: string, enc: 'utf8'): string
  readdirSync(p: string): string[]
  statSync(p: string): { isDirectory(): boolean }
}
let fs: Fs
beforeAll(async () => { fs = (await import(/* @vite-ignore */ 'node:' + 'fs')) as Fs })
const SITE = new URL('../../', (import.meta as unknown as { url: string }).url).pathname
const read = (rel: string) => fs.readFileSync(SITE + rel, 'utf8')

function tsFiles(dir: string): string[] {
  return fs.readdirSync(dir).flatMap(f => {
    const p = `${dir}/${f}`
    if (fs.statSync(p).isDirectory()) return f === 'node_modules' ? [] : tsFiles(p)
    return p.endsWith('.ts') && !p.endsWith('.test.ts') && !p.endsWith('.d.ts') ? [p] : []
  })
}

/** Field names of every `interface …Env {…}` / `type …Env = … & {…}` block. */
export function envFields(src: string): string[] {
  const out: string[] = []
  const head = /(?:interface\s+\w*Env\w*\s*\{|type\s+\w*Env\w*\s*=[^{\n]*\{)/g
  for (let m; (m = head.exec(src));) {
    let depth = 1, i = head.lastIndex
    while (depth && i < src.length) depth += src[i] === '{' ? 1 : src[i] === '}' ? -1 : 0, i++
    const body = src.slice(head.lastIndex, i - 1).replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')
    for (const f of body.matchAll(/(?:^|[{;,\s])([A-Z][A-Z0-9_]*)\??\s*:/g)) out.push(f[1])
  }
  return out
}

/** Every key the example declares: vars (set or commented out), bindings, and
 * the `# secret` / `# builtin` / `# dev` / `# overlay` lines. */
export function exampleKeys(toml: string): { all: string[]; vars: string[] } {
  const vars = [...toml.matchAll(/^#?\s*([A-Z][A-Z0-9_]*) = /gm)].map(m => m[1])
  const bindings = [...toml.matchAll(/^#?\s*binding = "(\w+)"/gm)].map(m => m[1])
  const marked = [...toml.matchAll(/^# (?:secret|builtin|dev|overlay) ([A-Z][A-Z0-9_]*)/gm)].map(m => m[1])
  return { all: [...vars, ...bindings, ...marked], vars }
}

describe('envFields / exampleKeys', () => {
  it('reads interface and intersection-type fields, skipping comments', () => {
    expect(envFields('interface Env {\n  DB?: D1Database\n  /** A: b */\n  STORE_KEY?: string\n}\ntype ExecEnv = A & { GCP_SA_KEY?: string; JOB_SA: string }\ninterface Other { NOPE: 1 }'))
      .toEqual(['DB', 'STORE_KEY', 'GCP_SA_KEY', 'JOB_SA'])
  })
  it('reads set and commented vars, bindings and marked lines', () => {
    expect(exampleKeys('STORE = "x"\n# STAGING = "1"   # any\nbinding = "DB"\n# binding = "CACHE_KV"\n# secret SESSION_SECRET  signs\n# dev DEV_EMAIL\n# some prose = no'))
      .toEqual({ all: ['STORE', 'STAGING', 'DB', 'CACHE_KV', 'SESSION_SECRET', 'DEV_EMAIL'], vars: ['STORE', 'STAGING'] })
  })
})

describe('wrangler.example.toml', () => {
  const load = () => ({
    fields: [...new Set(tsFiles(SITE + 'functions').flatMap(f => envFields(fs.readFileSync(f, 'utf8'))))].sort(),
    build: [...new Set([...read('vite.config.ts').matchAll(/VARS\.([A-Z][A-Z0-9_]*)/g)].map(m => m[1]))],
    ex: exampleKeys(read('wrangler.example.toml')),
  })
  it('mentions every binding, var and secret the Functions read', () => {
    const { fields, ex } = load()
    expect(fields.filter(k => !ex.all.includes(k))).toEqual([])
  })
  it('sets no var that nothing reads', () => {
    const { fields, build, ex } = load()
    expect(ex.vars.filter(k => !fields.includes(k) && !build.includes(k))).toEqual([])
  })
})
