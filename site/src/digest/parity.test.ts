/** The site's OP renderer against the job's: each `cloud/tests/fixtures/digest/parity/*.json` (written by
 * `cloud/tests/test_digest_parity.py`) holds a month's rows and config with the OP text and plot request the job
 * rendered from them; the TS renderer must produce both exactly. */
import { describe, expect, it } from 'vitest'
import { type DigestCfg, type DigestRows, imageRequest, opText } from './index'

interface Fs { readdir(p: string): Promise<string[]>; readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const DIR = new URL('../../../cloud/tests/fixtures/digest/parity/', import.meta.url).pathname

interface Fixture { cfg: DigestCfg; month: string; plot_url: string | null; prev_url: string | null; rows: DigestRows; op: string; image: Record<string, unknown> }

const fixtures = async (): Promise<[string, Fixture][]> => {
  const fs = await fsp()
  const names = (await fs.readdir(DIR)).filter(n => n.endsWith('.json')).sort()
  return Promise.all(names.map(async n => [n, JSON.parse(await fs.readFile(DIR + n, 'utf8'))] as [string, Fixture]))
}

describe('digest OP parity with the job', () => {
  it('covers every fixture the job writes', async () => {
    expect((await fixtures()).map(([n]) => n)).toEqual([
      'cw-first.json', 'cw-noquota.json', 'cw-sep.json', 'cw-tb.json', 'cw-two-weeks.json', 'cw6-oct.json', 'gcs-aug-discord.json',
      'gcs-aug.json', 'gcs-first.json',
    ])
  })
  it('renders each fixture\'s OP text byte for byte', async () => {
    for (const [n, f] of await fixtures()) {
      expect([n, opText(f.rows, f.month, f.cfg, { plotUrl: f.plot_url, prevUrl: f.prev_url })]).toEqual([n, f.op])
    }
  })
  it('builds each fixture\'s plot request', async () => {
    for (const [n, f] of await fixtures()) expect([n, imageRequest(f.rows, f.month, f.cfg)]).toEqual([n, f.image])
  })
})
