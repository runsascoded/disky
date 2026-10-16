import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from '../_lib/testStore'
import { searchKey } from '../_lib/search'
import { type FilterRejectCode, REJECT_MESSAGES, rejectQuery } from '../_lib/indexedOnly'
import { KIND_ERROR } from '../_lib/querySyntax'
import { parseQuery } from '../_lib/scope'
import { brute, fixtureObjects, SCANS } from '../_lib/testBrute'
import { drillSource, type HitSource, injectedStores, SuffixHits, type StaticFilterStore } from '../_lib/staticFilter'
import { type Blobs, scanAt, StaticNames } from '../_lib/staticNames'
import { StaticCatalog } from '../_lib/staticCatalog'
import { AnchoredSource } from '../_lib/staticAnchors'
import type { Tiers } from '../_lib/staticRuns'
import { onRequestGet as subtree } from './subtree'
import { onRequestGet as diff } from './diff'
import { onRequestGet as series } from './series'
import { onRequestGet as caps } from './filter-caps'
import { onRequestGet as filterScans } from './filter-scans'
import { onRequestGet as filterCover } from './filter-cover'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `FILTER_INDEXED_ONLY=1` (`_lib/indexedOnly.ts`): the map's filter (subtree, diff, series) takes literal
// substrings of names — one, or AND / OR / NOT of them (`staticBool.ts`) — unscoped, on a scan the static index
// covers; every other form, scope or scan is a 400 `{ error, code }` before any read (a combination the index
// can't answer exactly, `too-broad`); unset, the same requests answer as before. Over the static-filter
// fixture's two scans (`fixtures/static-filter/`) and its drill (`fixtures/static-drill/`).

describe('D1', () => {
  const A = '2026-10-04'
  const B = '2026-10-05'
  const dirOf = (date: string) => `cw-l2/${date}/index/g`
  let base: Env
  const filterFiles = new Map<string, ArrayBuffer>()
  const drillFiles = new Map<string, ArrayBuffer>()

  function blobsOf(files: Map<string, ArrayBuffer>, override: Record<string, unknown> = {}): Blobs {
    const bytes = (k: string) => { const b = files.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
    return {
      async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
      async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
      async json(key) { return key in override ? override[key] : JSON.parse(new TextDecoder().decode(bytes(key))) },
    }
  }
  async function readTree(dir: string, into: Map<string, ArrayBuffer>, rel = ''): Promise<void> {
    const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
    for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
      const k = rel ? `${rel}/${e.name}` : e.name
      if (e.isDirectory()) await readTree(`${dir}/${e.name}`, into, k)
      else { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); into.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
    }
  }

  /** The static store: the suffix index on `scans`, literals over 40 suffix rows from the drill, whose base
   *  generation is `drillScans`. */
  function store(scans = [A, B], drillScans = [A, B], dirOnly: string[] = []): StaticFilterStore {
    const heavy = drillSource(blobsOf(drillFiles, { 'scans.json': { scans: drillScans.map(id => ({ id })) } }))
    return { source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 40, heavy }), scans: async () => scans, dirOnly: async () => dirOnly, gen: 'fixture' }
  }
  /** `paths`: `SEARCH_PATH_TERMS=1` (path terms answered; unset, parked). */
  const envOf = (flag: boolean, s: StaticFilterStore = store(), paths = false): Env => {
    const env = { ...base, ...(flag ? { FILTER_INDEXED_ONLY: '1' } : {}), ...(paths ? { SEARCH_PATH_TERMS: '1' } : {}) } as Env
    injectedStores.set(env, s)
    return env
  }

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    await readTree('static-drill', drillFiles)
    await readTree('static-filter/sx', filterFiles, 'sx')
    for (const k of ['shards.json', 'scans.json']) { const m = new Map<string, ArrayBuffer>(); await readTree('static-filter', m); filterFiles.set(k, m.get(k)!) }
    const { db, raw } = await sqliteD1()
    for (const date of [A, B]) {
      const v = await readJson<Record<string, D1Variant>>(`static-filter/${date}/d1.json`)
      const files = {
        path: { parquet: `static-filter/${date}/path-index.parquet`, groups: `static-filter/${date}/path-index.groups.json` },
        bysize: { parquet: `static-filter/${date}/path-index-bysize.parquet`, groups: `static-filter/${date}/path-index-bysize.groups.json` },
      }
      seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
      for (const [role, file] of [['rows', 'rows'], ['trigrams', 'trigrams'], ['rowsSearch', 'rows-search']] as const) {
        FILES.set(searchKey(dirOf(date), role), fixture(`static-filter/${date}/path-index.${file}.parquet`))
      }
    }
    base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', SEARCH_SIDECARS: '1', STORE_BUCKET: 'my-data' } as Env
  })

  type Route = typeof subtree
  const call = async (route: Route, qs: string, env: Env): Promise<[number, unknown]> => {
    const r = await route({ request: new Request(`http://localhost/api/x?${qs}`), env })
    const text = await r.text()
    return [r.status, r.headers.get('content-type')?.startsWith('application/json') && r.status === 400 ? JSON.parse(text) : r.status === 200 ? 'ok' : text]
  }
  const refusal = (code: FilterRejectCode) => [400, { error: REJECT_MESSAGES[code], code }]

  /** Every rejected form, its code. */
  const FORMS: [string, string, FilterRejectCode][] = [
    ['`is:file`', `q=${encodeURIComponent('tomat is:file')}`, 'unsupported-kind'],
    ['a user lens', 'q=tomat&lens=user:alice', 'unsupported-scope'],
    ['a class scope', 'q=tomat&cl=s', 'unsupported-scope'],
  ]

  describe('rejectQuery: what an indexed-only deployment refuses', () => {
    it('literals (any length, quoted spaces too) and AND / OR / NOT of them pass — `*`, `?` and regex characters are text; every other form has its code', () => {
      const code = (q: string, paths = false) => rejectQuery(q, paths)?.code ?? null
      expect([
        code('tomat'), code('TOMAT'), code('.'), code('ab'), code('"final ckpt"'), code(''), code('  '),
        code('tomat -bin'), code('-ttl'), code('a b'), code('a|b'), code('a b|c -d'), code('"is:file" x'),
        code('ckpt.*pt'), code('tom*t'), code('a -b*c'), code('*'), code('a?b'), code('(x)+[y]'),
        code('/ckpt/'), code('data/tomat'), code('a|b/c'), code('a/bc$|x'), code('x -a/b'), code('"a/b"'), code('is:file'), code('-is:dir x'),
        // Path terms on (`SEARCH_PATH_TERMS`): read from the name index's tail range, which needs the tail.
        code('/ckpt/', true), code('data/tomat', true), code('a|b/c', true), code('a/bc$|x', true), code('x -a/b', true),
      ]).toEqual([
        null, null, null, null, null, null, null,
        null, null, null, null, null, null,
        null, null, null, null, null, null,
        'unsupported-slash', 'unsupported-slash', 'unsupported-slash', 'unsupported-slash', 'unsupported-slash', 'unsupported-slash', 'unsupported-kind', 'unsupported-kind',
        'slash-tail-short', null, 'slash-tail-short', null, 'slash-tail-short',
      ])
    })
    it('one anchored literal passes (`^q` from 2 characters, `q$` from 3, `^q$` from 1); shorter ones, globs and slashes don\'t', () => {
      const code = (q: string, paths = false) => rejectQuery(q, paths)?.code ?? null
      expect([
        code('^train'), code('.json$'), code('^config.json$'), code('^ab'), code('.gz$'), code('^x$'), code('"^a"'), code('^TRAIN'),
        code('^ckpt -tmp'), code('^ab .json$'), code('x -.gz$|^ab'),
        code('^a'), code('gz$'), code('^ab*cd'), code('^a/b'), code('^a .json$'), code('x -gz$'), code('^a/b', true),
      ]).toEqual([
        null, null, null, null, null, null, null, null,
        null, null, null,
        'anchor-too-short', 'anchor-too-short', null, 'unsupported-slash', 'anchor-too-short', 'anchor-too-short', 'slash-tail-short',
      ])
    })
  })

  describe('escaped `^`/`$`: a plain literal on an indexed-only deployment', () => {
    it('`\^q`, `q\$`, `\\` and the quoted forms pass as literals, whatever their length; a bare anchor is still one', () => {
      const code = (q: string) => rejectQuery(q)?.code ?? null
      expect([
        code('\\^tomat'), code('tomat\\$'), code('\\^a'), code('a\\$'), code('\\^tomat\\$'), code('a\\\\b'), code('"^a"'), code('"a$"'),
        code('^a\\$'), code('^a'), code('a$'), code('\\^a*b'), code('\\^a -b'),
      ]).toEqual([
        null, null, null, null, null, null, null, null,
        null, 'anchor-too-short', 'anchor-too-short', null, null,
      ])
    })
    it('the map routes, flag set: subtree, diff and series answer them', async () => {
      const got = []
      for (const q of ['tomat\\$', '\\^tomat', '"^tomat"']) {
        const qs = `q=${encodeURIComponent(q)}`
        got.push(await Promise.all([
          call(subtree, `date=${A}&path=bk&${qs}`, envOf(true)),
          call(diff, `from=${A}&to=${B}&path=bk&${qs}`, envOf(true)),
          call(series, `path=bk&${qs}`, envOf(true)),
        ]))
      }
      expect(got).toEqual(Array(3).fill([[200, 'ok'], [200, 'ok'], [200, 'ok']]))
    })
  })

  describe('`plain`: every character is text, `qs=` doesn\'t apply', () => {
    /** A subtree body, its timing-free fields. */
    const body = async (qs: string) => {
      const r = await subtree({ request: new Request(`http://localhost/api/x?${qs}`), env: envOf(true) })
      return [r.status, await r.json()]
    }
    it('`*`, `?` and regex characters search those characters (nothing here holds them): an empty view, no refusal', async () => {
      const got = await Promise.all(['tom*t', 'tomat?', '(tomat)'].map(async q => {
        const [status, j] = await body(`date=${A}&path=bk&q=${encodeURIComponent(q)}`) as [number, { tier: string; tree: { b: number }; matches: string[] }]
        return [q, status, j.tier, j.tree.b, j.matches]
      }))
      expect(got).toEqual([
        ['tom*t', 200, 'none', 0, []],
        ['tomat?', 200, 'none', 0, []],
        ['(tomat)', 200, 'none', 0, []],
      ])
    })
    it('`qs=regex` / `qs=simple` are ignored: the same body as none', async () => {
      const q = `date=${A}&path=bk&q=tomat`
      const [plain, regex, simple] = await Promise.all([body(q), body(`${q}&qs=regex`), body(`${q}&qs=simple`)])
      expect([regex, simple]).toEqual([plain, plain])
    })
  })

  describe('owner pools (`o=`): the static hits are owner slices, so a pool applies; a rollup has none', () => {
    const enc = (t: string) => `q=${encodeURIComponent(t)}`
    const POOLS: [string, (u: string | null) => boolean][] = [['owned', u => u != null], ['unowned', u => u == null], ['!alice', u => u != null && u !== 'alice']]
    const bodyOf = async (route: Route, qs: string, env: Env) => {
      const r = await route({ request: new Request(`http://localhost/api/x?${qs}`), env })
      return [r.status, await r.json()]
    }
    it('flag set: subtree and diff answer exactly what the static view answers unflagged', async () => {
      const cases: [Route, string][] = []
      for (const [o] of POOLS) for (const t of ['tomat', 'tomat -bin']) for (const path of ['', 'bk']) {
        cases.push([subtree, `date=${A}&path=${path}&o=${o}&${enc(t)}`], [diff, `from=${A}&to=${B}&path=${path}&o=${o}&${enc(t)}`])
      }
      const [set, unset] = await Promise.all([true, false].map(flag => Promise.all(cases.map(([r, qs]) => bodyOf(r, qs, envOf(flag))))))
      expect(set.map(b => b[0])).toEqual(cases.map(() => 200))
      expect(set).toEqual(unset)
    })
    it('flag set: the series is each scan\'s in-pool matched total, as brute force', async () => {
      const cases: [string, string, string][] = []
      for (const [o] of POOLS) for (const t of ['tomat', 'tomat -bin']) cases.push([o, t, 'bk'])
      const got = await Promise.all(cases.map(async ([o, t, root]) => {
        const r = await series({ request: new Request(`http://localhost/api/series?path=${root}&o=${o}&${enc(t)}`), env: envOf(true) })
        const j = await r.json() as { points: { date: string; b: number; o: number }[] }
        return [r.status, j.points.map(p => [p.date, p.b, p.o])]
      }))
      expect(got).toEqual(cases.map(([o, t, root]) => [200, SCANS.map(d => [d, ...brute(fixtureObjects(d), parseQuery(t)!, root, POOLS.find(p => p[0] === o)![1]).total])]))
    })
    it('a heavy literal\'s rollup (`f00` under `bk/fill`) under a pool: `owner-too-common` on every route', async () => {
      expect(refusal('owner-too-common')).toEqual([400, { error: 'This term is too common in this folder to split by owner here; open a folder, or clear the owner scope.', code: 'owner-too-common' }])
      expect(await Promise.all([
        call(subtree, `date=${A}&path=bk/fill&o=unowned&${enc('f00')}`, envOf(true)),
        call(diff, `from=${A}&to=${B}&path=bk/fill&o=unowned&${enc('f00')}`, envOf(true)),
        call(series, `path=bk/fill&o=unowned&${enc('f00')}`, envOf(true)),
      ])).toEqual([refusal('owner-too-common'), refusal('owner-too-common'), refusal('owner-too-common')])
    })
  })

  describe('path terms parked (`SEARCH_PATH_TERMS` unset): a term holding `/` is `unsupported-slash` up front', () => {
    const enc = (t: string) => `q=${encodeURIComponent(t)}`
    /** A store whose every read fails: a refusal before any read passes, anything else throws. */
    const unread: StaticFilterStore = {
      source: { hits: async () => { throw new Error('read') } },
      scans: async () => { throw new Error('read') },
      gen: 'unread',
    }
    const TERMS = ['data/tomat', '^data/tomat$', 'tomat|data/raw', 'tomat -data/raw', 'data/', '/', 'data/t', '/tomat/', 'Data/Raw//', 'a b/c']
    it('on every route (subtree, diff, series, filter-cover), alone or in AND / OR / NOT, whatever the tail; nothing read', async () => {
      const got = []
      for (const t of TERMS) {
        got.push(await Promise.all([
          call(subtree, `date=${A}&path=bk&${enc(t)}`, envOf(true, unread)),
          call(diff, `from=${A}&to=${B}&path=bk&${enc(t)}`, envOf(true, unread)),
          call(series, `path=bk&${enc(t)}`, envOf(true, unread)),
          call(filterCover as never, `date=${A}&path=bk&${enc(t)}`, envOf(true, unread)),
        ]))
      }
      expect(got).toEqual(TERMS.map(() => Array(4).fill(refusal('unsupported-slash'))))
    })
    it('says so in words', () => {
      expect(refusal('unsupported-slash')).toEqual([400, { error: 'Only text search (substrings of file or folder names, combined with spaces, | and -) is supported here; a term can’t contain “/” (it matches within one name).', code: 'unsupported-slash' }])
    })
  })

  describe('path terms (`a/b`, `SEARCH_PATH_TERMS=1`): read from the name index (`staticAnchors.ts`)', () => {
    const enc = (t: string) => `q=${encodeURIComponent(t)}`
    it('this fixture has none: `anchor-not-indexed` on every route, alone or in a compound; a tail too short is `slash-tail-short`', async () => {
      const got = []
      for (const t of ['data/tomat', 'tomat|data/raw', 'tomat -data/raw', 'data/t', 'data/t -x']) {
        got.push(await Promise.all([
          call(subtree, `date=${A}&path=bk&${enc(t)}`, envOf(true, store(), true)),
          call(diff, `from=${A}&to=${B}&path=bk&${enc(t)}`, envOf(true, store(), true)),
          call(series, `path=bk&${enc(t)}`, envOf(true, store(), true)),
        ]))
      }
      expect(got).toEqual([
        ...Array(3).fill([refusal('anchor-not-indexed'), refusal('anchor-not-indexed'), refusal('anchor-not-indexed')]),
        ...Array(2).fill([refusal('slash-tail-short'), refusal('slash-tail-short'), refusal('slash-tail-short')]),
      ])
    })
    it('an empty tail (`x/`: what is inside folders ending in x) is `slash-tail-short` on every route, naming the folder term `x$` (its bytes, exact)', async () => {
      const folder = (term: string) => [400, { error: `A term ending in “/” (what’s inside a folder) isn’t indexed here; search for the folder itself: “${term}”.`, code: 'slash-tail-short' }]
      const got = []
      for (const t of ['^data/', 'data/', '/tomat/', 'Data/Raw//', 'tomat|data/']) {
        got.push(await Promise.all([
          call(subtree, `date=${A}&path=bk&${enc(t)}`, envOf(true, store(), true)),
          call(diff, `from=${A}&to=${B}&path=bk&${enc(t)}`, envOf(true, store(), true)),
          call(series, `path=bk&${enc(t)}`, envOf(true, store(), true)),
        ]))
      }
      expect(got).toEqual([
        Array(3).fill(folder('^data$')),
        Array(3).fill(folder('data$')),
        Array(3).fill(folder('/tomat$')),
        Array(3).fill(folder('data/raw$')),
        Array(3).fill(folder('data$')),
      ])
      // Nothing but slashes names no folder: the general wording.
      expect([rejectQuery('/', true), rejectQuery('^//', true)]).toEqual([
        { code: 'slash-tail-short', message: REJECT_MESSAGES['slash-tail-short'] },
        { code: 'slash-tail-short', message: REJECT_MESSAGES['slash-tail-short'] },
      ])
    })
    it('says so in words', () => {
      expect([refusal('slash-tail-short'), refusal('anchor-not-indexed')]).toEqual([
        [400, { error: 'After a term’s last “/”, type at least 2 characters of the name (e.g. “ckpt/mo”), or end it with “$” (“ckpt/m$”).', code: 'slash-tail-short' }],
        [400, { error: 'Starts-with (^), ends-with ($) and “/” search isn’t indexed on this deployment yet; search for a plain substring instead.', code: 'anchor-not-indexed' }],
      ])
    })
  })

  describe('anchor-too-short is worded for the anchor used', () => {
    it('`^q` says 2 after the caret, `q$` 3 before the dollar', () => {
      expect([rejectQuery('^a')?.message, rejectQuery('gz$')?.message]).toEqual([
        'A “^” term needs at least 2 characters after the “^” (e.g. “^ck”).',
        'A “$” term needs at least 3 characters before the “$” (add the dot: “.gz$”).',
      ])
    })
  })

  describe('the map routes, flag set: each rejected form is a 400 with its code; unset, the same request answers', () => {
    for (const [what, qs, code] of FORMS) {
      it(what, async () => {
        expect(await Promise.all([
          call(subtree, `date=${A}&path=bk&${qs}`, envOf(true)),
          call(diff, `from=${A}&to=${B}&path=bk&${qs}`, envOf(true)),
          call(series, `path=bk&${qs}`, envOf(true)),
        ])).toEqual([refusal(code), refusal(code), refusal(code)])
        // Unset: served (a series of a non-static query wants its roots, `paths=`, as before; the fixture has no
        // ledger for a lens read).
        if (code === 'unsupported-scope' && qs.includes('lens=')) return
        if (code === 'unsupported-kind') {
          expect(await call(subtree, `date=${A}&path=bk&${qs}`, envOf(false))).toEqual([400, `bad query: ${KIND_ERROR}`])
          return
        }
        const unset = await Promise.all([
          call(subtree, `date=${A}&path=bk&${qs}`, envOf(false)),
          call(diff, `from=${A}&to=${B}&path=bk&${qs}`, envOf(false)),
        ])
        expect(unset).toEqual([[200, 'ok'], [200, 'ok']])
      })
    }
  })

  describe('compound queries (AND / OR / NOT, `staticBool.ts`) on the map routes', () => {
    const enc = (t: string) => `q=${encodeURIComponent(t)}`
    const points = async (qs: string, env: Env) => {
      const r = await series({ request: new Request(`http://localhost/api/series?${qs}`), env })
      const j = await r.json() as { points?: { date: string; b: number; o: number }[]; unindexed?: string[]; error?: string; code?: string }
      return [r.status, j.points ? j.points.map(p => [p.date, p.b, p.o]) : j]
    }
    /** Brute force's matched total per scan (`testBrute.ts`). */
    const bruteSeries = (t: string, root: string) => SCANS.map(d => [d, ...brute(fixtureObjects(d), parseQuery(t)!, root).total])

    it('flag set: subtree, diff and series answer them', async () => {
      const got = []
      for (const t of ['tomat -bin', '-ttl', 'tomat bin', 'tomat|ckpt', 'data tomat|ckpt -inner']) {
        got.push(await Promise.all([
          call(subtree, `date=${A}&path=bk&${enc(t)}`, envOf(true)),
          call(diff, `from=${A}&to=${B}&path=bk&${enc(t)}`, envOf(true)),
          call(series, `path=bk&${enc(t)}`, envOf(true)),
        ]))
      }
      expect(got).toEqual(Array(5).fill([[200, 'ok'], [200, 'ok'], [200, 'ok']]))
    })
    it.each([[true], [false]])('the series (flag %s): each scan\'s matched total = brute force — roots less exclusions, or the view root less them', async flag => {
      const cases: [string, string][] = [['tomat -bin', 'bk'], ['tomat|ckpt', ''], ['data tomat|ckpt -inner', 'bk'], ['-ttl', 'bk/tmp'], ['-=7d', 'bk/tmp'], ['tmp -=7d', 'bk/tmp']]
      const got = await Promise.all(cases.map(([t, root]) => points(`path=${root}&${enc(t)}`, envOf(flag))))
      expect(got).toEqual(cases.map(([t, root]) => [200, bruteSeries(t, root)]))
    })
    it('too broad to combine exactly here (`f00` heavy under `bk/fill`, its roots past the rollup): flag set, `too-broad` on every route', async () => {
      expect(refusal('too-broad')).toEqual([400, { error: 'These terms match too much here to combine exactly; open a folder, or make a term more specific.', code: 'too-broad' }])
      expect(await Promise.all([
        call(subtree, `date=${A}&path=bk/fill&${enc('fil f00')}`, envOf(true)),
        call(diff, `from=${A}&to=${B}&path=bk/fill&${enc('fil f00')}`, envOf(true)),
        call(series, `path=bk/fill&${enc('fil f00')}`, envOf(true)),
        call(subtree, `date=${A}&path=bk&${enc('-f00')}`, envOf(true)),
      ])).toEqual([refusal('too-broad'), refusal('too-broad'), refusal('too-broad'), refusal('too-broad')])
    })
    it('…flag unset: the view reads as before (the search sidecars), flagged where that read may miss matches', async () => {
      const view = async (qs: string) => {
        const j = await (await subtree({ request: new Request(`http://localhost/api/x?${qs}`), env: envOf(false) })).json() as Record<string, unknown>
        return [(j.tier as string).split('+')[0], j.partial ?? null, j.approximate ?? null]
      }
      expect(await Promise.all([view(`date=${A}&path=bk/fill&${enc('fil f00')}`), view(`date=${A}&path=bk&${enc('tomat -bin')}`)]))
        .toEqual([['search', null, null], ['static', null, null]])
    })
    it('a term no source answers is that term\'s refusal: `0` with no heavy source is `term-too-common`', async () => {
      const light: StaticFilterStore = { source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 40 }), scans: async () => [A, B], gen: 'fixture' }
      expect(await call(subtree, `date=${A}&path=bk&${enc('tomat -0')}`, envOf(true, light))).toEqual(refusal('term-too-common'))
    })
    it('a scan outside the generation: `scan-not-indexed`, flag set or not (never the search / walk), on subtree and diff', async () => {
      expect(await Promise.all([
        call(subtree, `date=${B}&path=bk&${enc('tomat -bin')}`, envOf(true, store([A]))),
        call(subtree, `date=${B}&path=bk&${enc('tomat -bin')}`, envOf(false, store([A]))),
        call(subtree, `date=${B}&path=bk&${enc('tomat|ckpt')}`, envOf(false, store([A]))),
        call(diff, `from=${A}&to=${B}&path=bk&${enc('-ttl')}`, envOf(false, store([A]))),
        call(subtree, `date=${A}&path=bk&${enc('tomat -bin')}`, envOf(false, store([A]))),
      ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed'), refusal('scan-not-indexed'), refusal('scan-not-indexed'), [200, 'ok']])
    })
    it('a view root holding a negative term matches nothing: an empty view, no refusal', async () => {
      const j = await (await subtree({ request: new Request(`http://localhost/api/x?date=${A}&path=bk/tmp&${enc('bin -tmp')}`), env: envOf(true) })).json() as Record<string, unknown>
      expect([j.tier, (j.tree as { b: number }).b, j.matches]).toEqual(['none', 0, []])
    })
  })

  describe('the map routes, flag set: a heavy literal with no heavy source (`FILTER_STATIC_HEAVY` off)', () => {
    // the suffix index alone: a literal past its 40-row bound (or of one or two characters) has no source
    const light = (scans = [A, B]): StaticFilterStore => ({ source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 40 }), scans: async () => scans, gen: 'fixture' })
    it('says so in words', () => {
      expect(refusal('term-too-common')).toEqual([400, { error: 'This term matches too many files to search here; try a longer one.', code: 'term-too-common' }])
    })
    it('is `term-too-common` on an indexed scan — the term is the reason, not the scan', async () => {
      const env = envOf(true, light())
      expect(await Promise.all([
        call(subtree, `date=${A}&path=bk/fill&q=0`, env),
        call(subtree, `date=${B}&path=bk/fill&q=0`, env),
        call(diff, `from=${A}&to=${B}&path=bk/fill&q=0`, env),
        call(subtree, `date=${A}&path=bk&q=tomat`, env),
      ])).toEqual([refusal('term-too-common'), refusal('term-too-common'), refusal('term-too-common'), [200, 'ok']])
    })
    it('a scan outside the generation is still `scan-not-indexed`, whatever the term', async () => {
      const env = envOf(true, light([A]))
      expect(await Promise.all([
        call(subtree, `date=${B}&path=bk/fill&q=0`, env),
        call(subtree, `date=${B}&path=bk&q=tomat`, env),
      ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed')])
    })
  })

  describe('the map routes, flag unset, no drilldown: a heavy literal is the catalog\'s buckets at the root, refused below', () => {
    // as `staticFilterStore` wires `FILTER_STATIC_HEAVY` off: the suffix index (40-row bound) and the base catalog
    const noDrill = (): StaticFilterStore => ({ source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 40, catalog: new StaticCatalog(blobsOf(drillFiles)) }), scans: async () => [A, B], gen: 'fixture' })
    const body = async (route: Route, qs: string, env: Env) => (await route({ request: new Request(`http://localhost/api/x?${qs}`), env })).json() as Promise<Record<string, unknown>>
    it('subtree and diff below the root: `term-too-common` (not an approximate walk); a light literal answers', async () => {
      const env = envOf(false, noDrill())
      expect(await Promise.all([
        call(subtree, `date=${A}&path=bk/fill&q=0`, env),
        call(subtree, `date=${B}&path=bk&q=0`, env),
        call(diff, `from=${A}&to=${B}&path=bk/fill&q=0`, env),
        call(subtree, `date=${A}&path=bk&q=tomat`, env),
      ])).toEqual([refusal('term-too-common'), refusal('term-too-common'), refusal('term-too-common'), [200, 'ok']])
    })
    it('subtree at the root: the bucket totals, exact, flagged `bucketsOnly`, no approximate note', async () => {
      const j = await body(subtree, `date=${A}&path=&q=0`, envOf(false, noDrill()))
      const tree = j.tree as { b: number; o: number; c: { n: string; b: number; o: number }[] }
      expect([tree.b, tree.o, tree.c.map(c => [c.n, c.b, c.o]), j.matchCount, j.rollup, j.approximate, j.approximateReason])
        .toEqual([1023750, 3000, [['bk', 1023750, 3000]], { n: 0, b: 1023750, o: 3000 }, { children: 1, kept: 1, rows: null, bucketsOnly: true }, undefined, undefined])
    })
  })

  describe('the map routes, flag set: a scan the static index doesn\'t cover', () => {
    it('a heavy literal past the drill base: subtree and diff refuse, the covered scan answers', async () => {
      const env = envOf(true, store([A, B], [A]))
      expect(await Promise.all([
        call(subtree, `date=${B}&path=bk/fill&q=0`, env),
        call(diff, `from=${A}&to=${B}&path=bk/fill&q=0`, env),
        call(subtree, `date=${A}&path=bk/fill&q=0`, env),
      ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed'), [200, 'ok']])
    })

    it('…at the fleet root too: refused, never the catalog\'s or the walk\'s answer', async () => {
      const env = envOf(true, store([A, B], [A]))
      expect(await Promise.all([call(subtree, `date=${B}&path=&q=0`, env), call(diff, `from=${A}&to=${B}&path=&q=0`, env)]))
        .toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed')])
    })

    it('a scan outside the generation (even a light literal); unset, the same refusal', async () => {
      expect(await Promise.all([
        call(subtree, `date=${B}&path=bk&q=tomat`, envOf(true, store([A]))),
        call(subtree, `date=${B}&path=bk&q=tomat`, envOf(false, store([A]))),
      ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed')])
    })

    it('the series: covered scans\' points, the others named (`unindexed`), never read per scan', async () => {
      const r = await series({ request: new Request('http://localhost/api/series?path=bk/fill&q=0'), env: envOf(true, store([A, B], [A])) })
      const j = await r.json() as { points: { date: string }[]; unindexed?: string[] }
      expect([r.status, j.points.map(p => p.date), j.unindexed]).toEqual([200, [A], [B]])
    })

    // Unset, the same: a scan the static answer doesn't cover is a gap named in `unindexed` — never the current
    // scan's roots read on it, nor a zero. A dir-only (v1) scan the generation holds is one too: its files are invisible.
    it.each([[true], [false]])('the series (flag %s): uncovered and dir-only scans are gaps, named', async flag => {
      const got = async (qs: string, s: StaticFilterStore) => {
        const r = await series({ request: new Request(`http://localhost/api/series?${qs}`), env: envOf(flag, s) })
        const j = await r.json() as { points: { date: string; b: number }[]; unindexed?: string[] }
        return [r.status, j.points.map(p => p.date), j.unindexed ?? null]
      }
      expect(await Promise.all([
        got('path=bk/fill&q=0', store([A, B], [A])),
        got('path=bk&q=tomat', store([A, B], [A, B], [A])),
        got('path=bk&q=tomat', store([A, B])),
      ])).toEqual([
        [200, [A], [B]],
        [200, [B], [A]],
        [200, [A, B], null],
      ])
    })

    it('a literal the view root holds is the plain view (nothing to search), on any scan', async () => {
      expect(await call(subtree, `date=${B}&path=bk/data/tomato&q=tomat`, envOf(true, store([A])))).toEqual([200, 'ok'])
    })

    it('a supported literal answers byte for byte as unset', async () => {
      const [on, off] = await Promise.all([true, false].map(async f => (await subtree({ request: new Request(`http://localhost/api/x?date=${A}&path=bk&q=tomat`), env: envOf(f) })).text()))
      expect(on).toEqual(off)
    })
  })

  describe('an anchored term on a generation with no anchors build (cw: `anchors=False`)', () => {
    /** The static-filter fixture's light index (no `anchors/meta.json`) with the anchored reader wired as
     *  `staticFilterStore` wires it; `maxRows` 0 makes every `q$` heavy. The fixture predates `catalog/`, so its
     *  one light tier is handed to the anchored reader directly (`Tiers.state` would find none usable). */
    const anchorless = (maxRows?: number): StaticFilterStore => {
      const b = blobsOf(filterFiles), names = new StaticNames(b)
      const light = { state: async () => ({ version: 'base', scans: [A, B], tiers: [{ dir: null, names }], manifest: null, hexRuns: null }) } as unknown as Tiers
      return { source: new SuffixHits(names, { maxRows: 40, heavy: drillSource(blobsOf(drillFiles, { 'scans.json': { scans: [A, B].map(id => ({ id })) } })), anchored: new AnchoredSource(light, b, { maxRows }) }), scans: async () => [A, B], gen: 'fixture' }
    }
    const q = (term: string) => `q=${encodeURIComponent(term)}`
    it('says so in words', () => {
      expect(refusal('anchor-not-indexed')).toEqual([400, { error: 'Starts-with (^), ends-with ($) and “/” search isn’t indexed on this deployment yet; search for a plain substring instead.', code: 'anchor-not-indexed' }])
    })
    it.each([[false], [true]])('`^q`, `^q$` and a heavy `q$` are `anchor-not-indexed` on subtree, diff and series (flag %s), never the walk', async flag => {
      const env = envOf(flag, anchorless(0))
      const got = await Promise.all(['^tomat', '^tomato$', 'mato$'].flatMap(term => [
        call(subtree, `date=${A}&path=bk&${q(term)}`, env),
        call(diff, `from=${A}&to=${B}&path=bk&${q(term)}`, env),
        call(series, `path=bk&${q(term)}`, env),
      ]))
      expect(got).toEqual(Array(9).fill(refusal('anchor-not-indexed')))
    })
    it('a light `q$` answers from the light index, exactly (no approximate note)', async () => {
      const j = await (await subtree({ request: new Request(`http://localhost/api/x?date=${A}&path=bk&${q('mato$')}`), env: envOf(false, anchorless()) })).json() as Record<string, unknown>
      expect([j.approximate, j.approximateReason, (j.matchCount as { n: number }).n > 0]).toEqual([undefined, undefined, true])
    })
    it('mutation: without the anchorless check, `^q` falls through to the path-store walk (the bug) and is answered', async () => {
      const spy = vi.spyOn(AnchoredSource.prototype as unknown as { anchorless(): Promise<boolean> }, 'anchorless').mockResolvedValue(false)
      try {
        const env = envOf(false, anchorless(0))
        expect(await Promise.all([call(subtree, `date=${A}&path=bk&${q('^tomat')}`, env), call(series, `path=bk&${q('^tomat')}`, env)]))
          .toEqual([[200, 'ok'], [400, { error: 'a filtered series needs its match roots (paths=)' }]])
      } finally { spy.mockRestore() }
    })
  })

  // cw prod (`FILTER_STATIC` on, `FILTER_INDEXED_ONLY` off): its newest scan, published but not yet appended to the
  // static index, answered `?f=.json` from the thresholded path-store walk — "no matches" — blind to small matches.
  describe('flag unset: a static literal on a scan the generation doesn\'t hold is `scan-not-indexed`, never the walk', () => {
    const newer = () => envOf(false, store([A]))
    const older = () => envOf(false, store([B]))
    const coverOf = async (qs: string, env: Env): Promise<[number, unknown]> => {
      const r = await filterCover({ request: new Request(`http://localhost/api/filter-cover?${qs}`), env } as never)
      const j = await r.json() as Record<string, unknown>
      return [r.status, r.status === 200 ? [j.date, j.roots] : j]
    }
    const tierOf = async (qs: string, env: Env): Promise<[number, string]> => {
      const r = await subtree({ request: new Request(`http://localhost/api/x?${qs}`), env })
      return [r.status, (await r.json() as { tier: string }).tier.split('+')[0]]
    }

    it('subtree: past the newest scan and before the base, light and heavy literals, at the root and below', async () => {
      expect(await Promise.all([
        call(subtree, `date=${B}&path=bk&q=tomat`, newer()),
        call(subtree, `date=${B}&path=&q=tomat`, newer()),
        call(subtree, `date=${B}&path=bk&q=${encodeURIComponent('.json')}`, newer()),
        call(subtree, `date=${B}&path=bk/fill&q=0`, newer()),
        call(subtree, `date=${A}&path=bk&q=tomat`, older()),
      ])).toEqual(Array(5).fill(refusal('scan-not-indexed')))
    })

    it('diff: either side uncovered is refused; both covered answers', async () => {
      expect(await Promise.all([
        call(diff, `from=${A}&to=${B}&path=bk&q=tomat`, newer()),
        call(diff, `from=${A}&to=${B}&path=bk&q=tomat`, older()),
        call(diff, `from=${A}&to=${B}&path=bk&q=tomat`, envOf(false)),
      ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed'), [200, 'ok']])
    })

    it('filter-cover: refused on the uncovered scan; the covered one lists its roots', async () => {
      expect(await Promise.all([
        coverOf(`date=${B}&path=bk&q=tomat`, newer()),
        coverOf(`date=${A}&path=bk&q=tomat`, newer()),
      ])).toEqual([
        refusal('scan-not-indexed'),
        [200, [A, { n: 5, b: 17927, o: 7 }]],
      ])
    })

    it('a covered scan answers byte for byte as when the generation holds both', async () => {
      const [only, both] = await Promise.all([store([A]), store()].map(async s => (await subtree({ request: new Request(`http://localhost/api/x?date=${A}&path=bk&q=tomat`), env: envOf(false, s) })).text()))
      expect([JSON.parse(only).tier.split('+')[0], only]).toEqual(['static', both])
    })

    it('a query the static index can\'t answer (regex, glob) still walks the uncovered scan; a compound of literals is refused; a view root the literal holds is the plain view', async () => {
      expect(await Promise.all([
        tierOf(`date=${B}&path=bk&q=tom.*t&qs=regex`, newer()),
        tierOf(`date=${B}&path=bk&q=${encodeURIComponent('tom*t')}`, newer()),
        call(subtree, `date=${B}&path=bk&q=${encodeURIComponent('tomat bin')}`, newer()),
        call(subtree, `date=${B}&path=bk/data/tomato&q=tomat`, newer()),
      ])).toEqual([[200, 'bysize'], [200, 'search'], refusal('scan-not-indexed'), [200, 'ok']])
    })
  })

  describe('/api/filter-caps', () => {
    it('names the flag', async () => {
      const got = await Promise.all([envOf(true), envOf(false), envOf(true, store(), true), envOf(false, store(), true)].map(async env => (await caps({ request: new Request('http://localhost/api/filter-caps'), env } as never)).json()))
      expect(got).toEqual([
        { indexedOnly: true, rootLabel: 'root' },
        { indexedOnly: false, rootLabel: 'root' },
        // `SEARCH_PATH_TERMS=1`: path terms answered (an indexed-only box's concern only).
        { indexedOnly: true, pathTerms: true, rootLabel: 'root' },
        { indexedOnly: false, rootLabel: 'root' },
      ])
    })
    it('and the store root\'s label (`ROOT_LABEL`), the primary\'s or a secondary store\'s own', async () => {
      const env = { ...envOf(true), ROOT_LABEL: 'marin CoreWeave (dev)', STORES_JSON: JSON.stringify({ meta: { vars: { ROOT_LABEL: 'our storage' } } }) } as Env
      const got = await Promise.all(['', '?store=meta'].map(async qs => (await caps({ request: new Request(`http://localhost/api/filter-caps${qs}`), env } as never)).json()))
      expect(got).toEqual([{ indexedOnly: true, rootLabel: 'marin CoreWeave (dev)' }, { indexedOnly: true, rootLabel: 'our storage' }])
    })
    it('and the root crumb\'s tooltip (`ROOT_TITLE`), only where set', async () => {
      const env = { ...envOf(true), ROOT_LABEL: 'Marin CoreWeave', STORES_JSON: JSON.stringify({ meta: { vars: { ROOT_LABEL: 'Marin usage — meta', ROOT_TITLE: 'this site\'s own data' } } }) } as Env
      const got = await Promise.all(['', '?store=meta'].map(async qs => (await caps({ request: new Request(`http://localhost/api/filter-caps${qs}`), env } as never)).json()))
      expect(got).toEqual([{ indexedOnly: true, rootLabel: 'Marin CoreWeave' }, { indexedOnly: true, rootLabel: 'Marin usage — meta', rootTitle: 'this site\'s own data' }])
    })
  })

  describe('/api/filter-scans', () => {
    it('lists the static index\'s covered scans (ascending), or null without a static filter', async () => {
      const plain = { ...base } as Env
      const got = await Promise.all([envOf(true, store([B, A])), plain].map(async env => {
        const r = await filterScans({ request: new Request('http://localhost/api/filter-scans'), env } as never)
        return [r.status, await r.json()]
      }))
      expect(got).toEqual([[200, { scans: [A, B] }], [200, { scans: null }]])
    })
  })

  describe('a dir-only (v1) scan: its folder-name matches, flagged — never a complete answer', () => {
    // The fixture's index saw both scans whole; a v1 index on `A` would never have seen a file. `asV1` replays that:
    // a file's versions live on `A` start at `B` instead (dropped if that leaves them empty). The fixture's files are
    // exactly its paths whose name has an extension (`fixtures/static-filter/gen.py`'s `BASE`).
    const isFile = (p: string) => /\.[a-z]+$/i.test(p.slice(p.lastIndexOf('/') + 1))
    const asV1 = (inner: HitSource): HitSource => ({
      heavy: inner.heavy,
      async hits(key, under, opts) {
        const f = await inner.hits(key, under, opts)
        if (!f?.hits) return f
        const a = scanAt(A), b = scanAt(B)
        return { ...f, hits: f.hits.flatMap(h => !isFile(h.path) || !(h.vf <= a && a < h.vt) ? [h] : h.vt > b ? [{ ...h, vf: b }] : []) }
      },
    })
    const v1 = (dirOnly: string[]): StaticFilterStore => { const s = store([A, B], [A, B], dirOnly); return { ...s, source: asV1(s.source) } }
    const view = async (qs: string, env: Env) => {
      const r = await subtree({ request: new Request(`http://localhost/api/x?${qs}`), env })
      const j = await r.json() as { matched: { path: string; b: number; o: number }[]; dirsOnly?: true; approximate?: true; partial?: true }
      return [r.status, j.matched.map(m => [m.path, m.b, m.o]), j.dirsOnly ?? null, j.approximate ?? null, j.partial ?? null]
    }

    it.each([[true], [false]])('a folder-name literal (flag %s): the folders that match, exact, flagged `dirsOnly`; the full scan unflagged', async flag => {
      const env = envOf(flag, v1([A]))
      expect(await Promise.all([view(`date=${A}&path=bk&q=tomat`, env), view(`date=${B}&path=bk&q=tomat`, env)])).toEqual([
        [200, [['bk/data/tomato', 8000, 2], ['bk/data/raw/tomat-1', 150, 2]], true, null, null],
        [200, [['bk/data/tomato', 10000, 3], ['bk/runs/x/ckpt-tomat.pt', 9500, 1], ['bk/new-tomat', 400, 1], ['bk/data/raw/tomat-1', 150, 2], ['bk/runs/x/tomat-tomat.bin', 77, 1]], null, null, null],
      ])
    })

    it.each([[true], [false]])('a file-name literal (flag %s): no folder matches, flagged — not a bare "no matches"', async flag => {
      const env = envOf(flag, v1([A]))
      expect(await Promise.all([view(`date=${A}&path=bk&q=ckpt`, env), view(`date=${B}&path=bk&q=ckpt`, env)])).toEqual([
        [200, [], true, null, null],
        [200, [['bk/runs/x/ckpt-tomat.pt', 9500, 1], ['bk/runs/x/ckpt-2.pt', 1200, 1]], null, null, null],
      ])
    })

    it.each([[true], [false]])('a diff (flag %s): across the cutover `scan-dirs-only`; both sides dir-only, flagged; neither, unflagged', async flag => {
      const flagOf = async (s: StaticFilterStore) => {
        const r = await diff({ request: new Request(`http://localhost/api/x?from=${A}&to=${B}&path=bk&q=tomat`), env: envOf(flag, s) })
        return r.status === 200 ? [200, (await r.json() as { dirsOnly?: true }).dirsOnly ?? null] : [r.status, await r.json()]
      }
      expect(await Promise.all([flagOf(v1([A])), flagOf(v1([B])), flagOf(store([A, B], [A, B], [A, B])), flagOf(store())])).toEqual([
        refusal('scan-dirs-only'), refusal('scan-dirs-only'), [200, true], [200, null],
      ])
    })

    it('a diff across the cutover whose view root the literal holds is the plain diff (nothing searched)', async () => {
      expect(await call(diff, `from=${A}&to=${B}&path=bk/data/tomato&q=tomat`, envOf(true, v1([A])))).toEqual([200, 'ok'])
    })

    it('a match action on a dir-only scan: no items, the reason given', async () => {
      const r = await filterCover({ request: new Request(`http://localhost/api/filter-cover?date=${A}&path=bk&q=tomat`), env: envOf(false, v1([A])) } as never)
      const j = await r.json() as Record<string, unknown>
      expect([r.status, j.items, j.complete, j.reason]).toEqual([200, [], false, 'This scan lists folders only, so its file matches can’t be listed here. Pick a newer scan to act on the matches.'])
    })

    it('says so in words', () => {
      expect(refusal('scan-dirs-only')).toEqual([400, { error: 'One of these scans lists folders only (files aren’t searchable on it), so a search can’t be compared across the two; compare two scans that both list files.', code: 'scan-dirs-only' }])
    })
  })
})
