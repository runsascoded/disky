/** The params `/admin/slack` varies, over a parity fixture (11 months of daily scans to 9/1; defaults pinned by
 * `./parity.test.ts`). */
import { describe, expect, it } from 'vitest'
import { DEFAULT_PARAMS, type DigestCfg, type DigestParams, type DigestRows, imageRequest, opText } from './index'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const load = async (n: string) =>
  JSON.parse(await (await fsp()).readFile(new URL(`../../../cloud/tests/fixtures/digest/parity/${n}.json`, import.meta.url).pathname, 'utf8')) as
    { cfg: DigestCfg; month: string; rows: DigestRows }

const links = { plotUrl: null, prevUrl: null }
const at = async (p: Partial<DigestParams>) => {
  const f = await load('cw-first')
  const q = { ...DEFAULT_PARAMS, ...p }
  return { lines: opText(f.rows, f.month, f.cfg, links, q).split('\n'), image: imageRequest(f.rows, f.month, f.cfg, q) }
}

describe('digest params', () => {
  it('window days: the look-back figure, the weeks and the plot span follow', async () => {
    const { lines, image } = await at({ windowDays: 14 })
    expect(lines[0].split(' · ')[1]).toBe('**+14.0 TiB** [last 14d](https://cw.example.org/?d=260901-14d#over-time)')
    expect([lines.filter(l => l.includes('[wk of')).map(l => l.split(']')[0]), image.from]).toEqual([[':arrow_deg20: [wk of 8/17', ':arrow_deg20: [wk of 8/24', ':arrow_deg0: [wk of 8/31'], '2026-08-19'])
  })
  it("headline: the figures, in the params' order", async () => {
    expect((await at({ headline: ['window', 'mtd'] })).lines[0].split(' · ').slice(0, 2).map(s => s.split('[')[1].split(']')[0]))
      .toEqual(['last 30d', 'month-to-date'])
    expect((await at({ headline: ['mtd'] })).lines[0].includes('last 30d')).toBe(false)
  })
  it("weeks: the month's only", async () => {
    expect((await at({ weeks: 'month' })).lines.filter(l => l.includes('[wk of')).map(l => l.split(']')[0])).toEqual([':arrow_deg0: [wk of 8/31'])
  })
  it('diff: month-to-date instead of the window', async () => {
    expect((await at({ diff: 'mtd' })).image.diff).toEqual({ from: '2026-08-31T0800', to: '2026-09-01T0800', path: 'main-data', title: 'What changed in main-data — 8/31 → 9/1' })
  })
  it("today's position: sent only when not the card's default", async () => {
    expect([(await at({})).image.today_at, (await at({ todayAt: [0.8, 1] })).image.today_at]).toEqual([undefined, [0.8, 1]])
  })
})
