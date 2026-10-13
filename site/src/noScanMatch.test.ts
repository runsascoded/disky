import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { fmtScan, fromMiss, scanMiss } from './scan'
import { hrefWithScan, NoScanMatch } from './NoScanMatch'
import { decodeSel } from './scanSlug'

// Newest first, as `scans.json` lists them: two scans on 10/9, a date-only 10/8.
const SCANS = ['2026-10-09T1236', '2026-10-09T0601', '2026-10-08']

describe('scanMiss / fromMiss — a page selection naming no scan', () => {
  it('the end slug: none while the list loads, none on a match, else the slug and its neighbours', () => {
    expect([
      scanMiss(decodeSel('26100903'), SCANS, false),
      scanMiss(decodeSel('261009'), SCANS, true),
      scanMiss(decodeSel('26100906'), SCANS, true),
      scanMiss(undefined, SCANS, true),
      scanMiss(decodeSel('-7d'), SCANS, true),
      scanMiss(decodeSel('26100903'), SCANS, true),
      scanMiss(decodeSel('261009-03'), SCANS, true),
      scanMiss(decodeSel('261010-7d'), SCANS, true),
      scanMiss(decodeSel('junk'), SCANS, true),
    ]).toEqual([
      null, null, null, null, null,
      { slug: '26100903', before: '2026-10-08', after: '2026-10-09T0601' },
      { slug: '26100903', before: '2026-10-08', after: '2026-10-09T0601' },
      { slug: '261010', before: '2026-10-09T1236', after: null },
      { slug: 'junk', before: null, after: null },
    ])
  })
  it('a pinned start matches among the scans before the end', () => {
    expect([
      fromMiss('2026-10-09', '2026-10-09T1236', SCANS),
      fromMiss('2026-10-09T12', '2026-10-09T1236', SCANS),
      fromMiss('2026-10-07', '2026-10-09T1236', SCANS),
      fromMiss(undefined, '2026-10-09T1236', SCANS),
    ]).toEqual([
      null,
      { slug: '26100912', before: '2026-10-09T0601', after: null },
      { slug: '261007', before: null, after: '2026-10-08' },
      null,
    ])
  })
})

describe('the "no scan matches" state', () => {
  it('links re-point ?d= at a neighbour, keeping the rest of the selection and page params', () => {
    expect([
      hrefWithScan('/marin-a', '?d=26100903&f=json', decodeSel('26100903'), '2026-10-09T0601'),
      hrefWithScan('/', '?d=26100903-7d', decodeSel('26100903-7d'), '2026-10-08'),
      hrefWithScan('/', '?d=2610091236-261007', decodeSel('2610091236-261007'), '2026-10-08', false),
      hrefWithScan('/', '?date=2026-10-07&from=2026-10-01', decodeSel('261007-261001'), '2026-10-08'),
      hrefWithScan('/users', '?d=junk', decodeSel('junk'), '2026-10-09T1236'),
    ]).toEqual([
      '/marin-a?d=2610090601&f=json',
      '/?d=2610080000-7d',
      '/?d=2610091236-2610080000',
      '/?d=2610080000-261001',
      '/users?d=2610091236',
    ])
  })
  it('renders the slug and both neighbours, or says there is none', () => {
    const html = (miss: Parameters<typeof NoScanMatch>[0]['miss']) => renderToStaticMarkup(createElement(MemoryRouter, null,
      createElement(NoScanMatch, { miss, hrefFor: s => `/?d=${s}` })))
    expect([
      html({ slug: '26100903', before: '2026-10-08', after: '2026-10-09T0601' }),
      html({ slug: 'junk', before: null, after: null }),
    ]).toEqual([
      `<p class="no-scan loading" role="alert">No scan matches <code>26100903</code>. Nearest: <a href="/?d=2026-10-08">← ${fmtScan('2026-10-08')}</a> · <a href="/?d=2026-10-09T0601">${fmtScan('2026-10-09T0601')} →</a></p>`,
      '<p class="no-scan loading" role="alert">No scan matches <code>junk</code>. No scan to offer instead.</p>',
    ])
  })
})
