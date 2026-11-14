import { describe, expect, it } from 'vitest'
import { compactQuietLines } from './compact'
import { type DigestCfg, type CwRows, type ReplyCfg, DEFAULT_PARAMS, opText, replies } from './index'

const TIB = 1024 ** 4
const quotas = [{ bytes: 100*TIB, name: '100 TiB', short: '100Ti', classes: [] }]
const cfg: DigestCfg = { template: 'cw', title: 'T', site_url: 'https://x.test', host: 'x.test', unit: { name: 'TiB', short: 'Ti', bytes: TIB }, primary: 'main', prices: {}, icons_base: '', buckets: {
  main: { label: 'main', path: null, zone: null, quotas }, west: { label: '04a', path: null, zone: null, quotas }, east: { label: '01a', path: null, zone: null, quotas: [] },
} }
const rc: ReplyCfg = { quota_marks: 'arrow-chip', total_headline: true, reply_lines: true, reply_hour: 12, provisional: false, alert_pct: 90, op_sender: {}, avatar_rev: 0, triggers: {} }
const rows: CwRows = { template: 'cw', closed: false, pre: [], lead: [], rows: [{ scan: '2026-09-23T1201', slug: '2609231201', tb: 50, extra: { west: 0, east: .1 }, classes: null }] }
const main = ':pool-chip-8-0: [main](https://x.test/main?d=2609231201#over-time): 50.0% of 100Ti (50.0 Ti free)'
const west = ':pool-chip-8-0: [04a](https://x.test/west?d=2609231201#over-time): 0%'
const east = '[01a](https://x.test/east?d=2609231201#over-time): 0 Ti'

describe('low-info digest entries', () => {
  it('retains each entry’s exact text and position of the first quiet entry', () => {
    expect(compactQuietLines([{ text: '04a: 0%', quiet: true }, { text: 'busy: 80%', quiet: false }, { text: '01a: 0 Ti', quiet: true }])).toEqual(['04a: 0% · 01a: 0 Ti', 'busy: 80%'])
  })
  it('matches the Python daily reply without summing unrelated quota pools', () => {
    expect(replies(rows, cfg, rc, 'sender').map(r => r.body)).toEqual([`${main}\n${west} · ${east}`])
  })
  it('shares low-info lines whatever their size', () => {
    const big = { ...rows, rows: [{ ...rows.rows[0], extra: { west: 0, east: 66 } }] }
    expect(replies(big, cfg, rc, 'sender').map(r => r.body)).toEqual([`${main}\n${west} · [01a](https://x.test/east?d=2609231201#over-time): 66 Ti`])
  })
  it('keeps a headroom clause on its own line', () => {
    const fuller = { ...rows, rows: [{ ...rows.rows[0], extra: { west: 30, east: .1 } }] }
    const west30 = ':pool-chip-8-0: [04a](https://x.test/west?d=2609231201#over-time): 30.0% of 100Ti (70.0 Ti free)'
    expect(replies(fuller, cfg, rc, 'sender').map(r => r.body)).toEqual([`${main}\n${west30}\n${east}`])
  })
  it('shares a line in the monthly OP as well', () => {
    const lines = opText(rows, '2026-09', cfg, { plotUrl: null, prevUrl: null }, { ...DEFAULT_PARAMS, layout: 'pools', quotas: 'off' }).split('\n')
    const start = lines.indexOf('*Buckets* (Δ month-to-date)') + 1
    expect(lines.slice(start, start+3)).toEqual([
      '[main](https://x.test/main?d=2609231201#over-time) 50 TiB · 50.0% of 100Ti (50.0 Ti free)',
      '[04a](https://x.test/west?d=2609231201#over-time) 0 TiB · 0% · [01a](https://x.test/east?d=2609231201#over-time) 0 TiB',
      '',
    ])
  })
})
