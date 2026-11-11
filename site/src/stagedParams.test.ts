import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  adoptSection, dropSection, EARLIER_KEY, FLAT_KEY, foldAll, foldOverride, foldsParam, type Folds, NO_FOLDS, pagesParam, runsPageParam, runsSizeParam,
  sectionOfHash, setFold, settledParam, settleFolds,
} from './stagedParams'

const folds = (base: Folds['base'], open: string[] = [], closed: string[] = []): Folds => ({ base, open, closed })

describe('foldsParam (`?x=`): which batches are open', () => {
  const cases: [Folds, string | undefined][] = [
    [NO_FOLDS, undefined],
    [folds('auto', ['12']), '12'],
    [folds('auto', [], ['7']), '_7'],
    [folds('auto', ['12', '15'], ['7']), '12.15_7'],
    [folds('auto', ['12', '13', '14', '20', EARLIER_KEY]), '12-14.20.e'],
    [folds('open'), '*'],
    [folds('open', [], ['7', '8']), '*_7-8'],
    [folds('closed'), '_*'],
    [folds('closed', ['12']), '12_*'],
  ]
  it('writes the default as nothing, a few batches as ids, "all" as `*` with its exceptions', () => {
    expect(cases.map(([f]) => foldsParam.encode(f))).toEqual(cases.map(([, s]) => s))
  })
  it('reads each back', () => {
    expect(cases.map(([, s]) => foldsParam.decode(s))).toEqual(cases.map(([f]) => f))
  })
  it('180 batches expanded but three is one short value', () => {
    const all = Array.from({ length: 180 }, (_, i) => String(i + 1))
    let f = foldAll(false)
    for (const k of ['7', '8', '90']) f = setFold(f, k, true, true)
    expect(foldsParam.encode(f)).toBe('*_7-8.90')
    expect(all.filter(k => foldOverride(f, k)).sort()).toEqual(['7', '8', '90'])
  })
  it('writes ids in order, once each, whatever order they were toggled in', () => {
    expect(foldsParam.encode(folds('auto', ['15', '3', '15', EARLIER_KEY, '4']))).toBe('3-4.15.e')
  })
  it('reads a hand-typed value leniently: unknown tokens dropped, a backwards or huge run ignored, both sides `*` nothing', () => {
    expect([
      foldsParam.decode('12.x.007'),
      foldsParam.decode('9-3.5'),
      foldsParam.decode('1-999999999'),
      foldsParam.decode('*_*'),
      foldsParam.decode('1_2_3'),
      foldsParam.decode(''),
      foldsParam.decode('3_3.4'),
    ]).toEqual([folds('auto', ['7', '12']), folds('auto', ['5']), NO_FOLDS, NO_FOLDS, NO_FOLDS, NO_FOLDS, folds('auto', ['3'], ['4'])])
  })
})

describe('folds: a toggle against the default, "all" and its exceptions', () => {
  it('an unlisted batch follows its completion', () => {
    expect([foldOverride(NO_FOLDS, '3'), foldOverride(folds('auto', ['3']), '3'), foldOverride(folds('auto', [], ['3']), '3')]).toEqual([undefined, false, true])
  })
  it('"expand all" / "collapse all" decide every batch, listed or not, but the exceptions', () => {
    expect(['3', '4'].map(k => foldOverride(folds('open', [], ['4']), k))).toEqual([false, true])
    expect(['3', '4'].map(k => foldOverride(folds('closed', ['4']), k))).toEqual([true, false])
  })
  it('toggling back to the default drops the batch from the URL', () => {
    const opened = setFold(NO_FOLDS, '3', false, true)
    expect([opened, setFold(opened, '3', true, true)]).toEqual([folds('auto', ['3']), NO_FOLDS])
    const closed = setFold(NO_FOLDS, '9', true, false)
    expect([closed, setFold(closed, '9', false, false)]).toEqual([folds('auto', [], ['9']), NO_FOLDS])
  })
  it('after "all", a toggle is an exception, and toggling back removes it', () => {
    const one = setFold(foldAll(false), '3', true, true)
    expect([one, setFold(one, '3', false, true)]).toEqual([folds('open', [], ['3']), folds('open')])
    const other = setFold(foldAll(true), '3', false, false)
    expect([other, setFold(other, '3', true, false)]).toEqual([folds('closed', ['3']), folds('closed')])
  })
  it('a state that matches every batch\'s default is the default: no param', () => {
    const groups: [string, boolean][] = [['1', false], ['2', true]]
    expect([
      settleFolds(foldAll(false), groups),
      settleFolds(setFold(foldAll(false), '2', true, true), groups),
      settleFolds(folds('auto', ['7']), groups),
    ]).toEqual([folds('open'), NO_FOLDS, NO_FOLDS])
  })
})

describe('pagesParam (`?pg=`): each staged table\'s page', () => {
  const cases: [Record<string, number>, string | undefined][] = [
    [{}, undefined],
    [{ [FLAT_KEY]: 2 }, '3'],
    [{ 12: 2 }, '12-3'],
    [{ 12: 2, 15: 1, [EARLIER_KEY]: 4 }, '12-3.15-2.e-5'],
    [{ [FLAT_KEY]: 1, 15: 1 }, '2.15-2'],
  ]
  it('writes only the tables off their first page, 1-based', () => {
    expect(cases.map(([p]) => pagesParam.encode(p))).toEqual(cases.map(([, s]) => s))
    expect(pagesParam.encode({ 12: 0, 15: 1 })).toBe('15-2')
  })
  it('reads each back, dropping what is not a page', () => {
    expect(cases.map(([, s]) => pagesParam.decode(s))).toEqual(cases.map(([p]) => p))
    expect([pagesParam.decode('1.12-1.x-3.15-0.4-2'), pagesParam.decode('')]).toEqual([{ 4: 1 }, {}])
  })
})

describe('settledParam (`?done`): the completed batches\' fold and its page', () => {
  it('closed is nothing, open a bare flag, a later page its number', () => {
    expect([null, 0, 2].map(settledParam.encode)).toEqual([undefined, '', '3'])
    expect([undefined, '', '3', '1', 'x', '0'].map(settledParam.decode)).toEqual([null, 0, 2, 0, 0, 0])
  })
})

describe('the runs table\'s page and size (`?rp=`, `?rn=`)', () => {
  it('a page is 1-based, absent when the table picks it', () => {
    expect([null, 0, 4].map(runsPageParam.encode)).toEqual([undefined, '1', '5'])
    expect([undefined, '1', '5', '0', 'x'].map(runsPageParam.decode)).toEqual([null, 0, 4, null, null])
  })
  it('a size is one of the menu\'s, 20 by default', () => {
    expect([20, 50, Infinity].map(runsSizeParam.encode)).toEqual([undefined, '50', 'all'])
    expect([undefined, '50', 'all', '20', '7', 'x'].map(runsSizeParam.decode)).toEqual([20, 50, Infinity, 20, 20, 20])
  })
})

describe('a `#` link to a section becomes `?v=`', () => {
  it('names a batch or the runs, nothing else', () => {
    expect(['#batch-12', '#runs', '#batch-earlier', '#batch-', '#dispatch', '', '#batch-12x'].map(sectionOfHash)).toEqual([
      { section: 'batch-12', batch: 12 }, { section: 'runs', batch: null }, null, null, null, null, null,
    ])
  })
  it('a batch link shows batches: the filter and the flat table go, the rest stays as written', () => {
    expect([
      adoptSection('', { section: 'batch-12', batch: 12 }),
      adoptSection('?q=hedy%7Cgrace&flat&s=-o&x=*_7', { section: 'batch-12', batch: 12 }),
      adoptSection('?v=batch-3&x=3', { section: 'batch-12', batch: 12 }),
      adoptSection('?q=hedy&run=a%2Fb', { section: 'runs', batch: null }),
    ]).toEqual(['?v=batch-12', '?s=-o&x=*_7&v=batch-12', '?x=3&v=batch-12', '?q=hedy&run=a%2Fb&v=runs'])
  })
})

it('the spy turned off: its section leaves the query, the rest as written', () => {
  expect(['?v=batch-12', '?q=a%7Cb&v=runs&x=*', '?x=*', ''].map(dropSection)).toEqual(['', '?q=a%7Cb&x=*', '?x=*', ''])
})

// A link carries a view, never a deletion: what a run acts on (the checked rows, the armed confirm, the bucket cut) is
// set by hand on the page, every time. Adding a URL param here means editing these lists, in sight of this rule.
describe('the URL holds /staged\'s view, never what a deletion acts on', () => {
  const src = (f: string) => readFileSync(join(__dirname, f), 'utf8')
  const keys = (f: string) => [...src(f).matchAll(/useUrlState\(\s*'([\w-]+)'/g)].map(m => m[1]).sort()
  it('the page and its runs read exactly these keys', () => {
    expect(keys('StagedPage.tsx')).toEqual(['d', 'done', 'flat', 'pg', 'q', 's', 'x'])
    expect(keys('StagedRuns.tsx')).toEqual(['order', 'rm', 'rn', 'rp', 'rq', 'rs', 'ru', 'run', 'sort'])
  })
  it('the selection, the armed confirm and the bucket cut are component state', () => {
    const page = src('StagedPage.tsx')
    const made = (name: string) => new RegExp(`const (?:\\[${name}, \\w+\\]|${name}) = (\\w+)[<(]`).exec(page)?.[1]
    expect(['armed', 'bucketsOff', 'sel'].map(made)).toEqual(['useState', 'useState', 'useRowSelection'])
    // The selection hook reads no URL: its only inputs are the rows on screen.
    expect([...src('rowSelection.ts').matchAll(/from '([^']+)'/g)].map(m => m[1])).toEqual(['react', 'use-kbd', 'use-kbd'])
  })
  // `?d=` is the one key that reaches a run's inputs (the rows that can be checked, the confirm's total): the page
  // dispatches nothing while it is sized at a scan the run doesn't read (`stagedScan.test.ts`, `plans.test.ts`).
  it('the scan: one rule turns both run controls off, hides an armed confirm, and is restated to every dispatch', () => {
    const page = src('StagedPage.tsx')
    const def = (name: string) => new RegExp(`const ${name} = ([^\\n]+)`).exec(page)?.[1]
    expect([def('date'), def('runScan'), def('noRun'), def('confirming')]).toEqual(['sizedScan(scans, pickedScan(dP, scans))', "scans[0] ?? ''", 'sizedAtOldScan(date, runScan)', 'armed && !noRun'])
    expect([...page.matchAll(/<RunButton off=\{(\w+)\} className="(\w+)"/g)].map(m => [m[1], m[2]])).toEqual([['noRun', 'dry'], ['noRun', 'danger']])
    // Every place the page arms or dispatches: each dispatch names the scan its run reads and the one the page is sized at.
    expect([...page.matchAll(/setArmed\(true\)|dispatch\.mutate\((?:\{ mode: '\w+', date: \w+, sized: \w+,)?/g)].map(m => m[0])).toEqual([
      "dispatch.mutate({ mode: 'dry', date: runScan, sized: date,",
      'setArmed(true)',
      "dispatch.mutate({ mode: 'real', date: runScan, sized: date,",
    ])
  })
})
