/** The replies' goldens: each `cloud/tests/fixtures/digest/replies/*.json` holds what the job's preview bundle carries
 * and the replies the job renders from it (pinned by `cloud/tests/test_digest_reply_parity.py`). */
import { describe, expect, it } from 'vitest'
import { type CwRows, type DigestCfg, type RenderedReply, type ReplyCfg, replies } from './index'
import { tokenize } from './mrkdwn'
import { ARROW_MARKS } from './pools'

interface Fs { readdir(p: string): Promise<string[]>; readFile(p: string, enc: 'utf8'): Promise<string> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>
const DIR = new URL('../../../cloud/tests/fixtures/digest/replies/', import.meta.url).pathname

interface Fixture { cfg: DigestCfg; rows: CwRows; reply: ReplyCfg; variant: string; replies: RenderedReply[] }

const fixtures = async (): Promise<[string, Fixture][]> => {
  const fs = await fsp()
  const names = (await fs.readdir(DIR)).filter(n => n.endsWith('.json')).sort()
  return Promise.all(names.map(async n => [n, JSON.parse(await fs.readFile(DIR + n, 'utf8'))] as [string, Fixture]))
}

describe('digest reply goldens', () => {
  it('covers every fixture', async () => {
    expect((await fixtures()).map(([n]) => n)).toEqual([
      'circles-sender.json', 'runway-closed-sender.json', 'runway-hue-sender.json', 'runway-sender.json', 'runway-total-sender.json',
      'solo-sender.json', 'warning-body.json', 'warning-sender.json',
    ])
  })
  it('renders each fixture\'s replies byte for byte', async () => {
    for (const [n, f] of await fixtures()) expect([n, replies(f.rows, f.cfg, f.reply, f.variant)]).toEqual([n, f.replies])
  })
  it('a candidate overrides the marks and the headline, and every arrow mark tokenizes to something drawn', async () => {
    const f = Object.fromEntries(await fixtures())['runway-sender.json']
    const total = Object.fromEntries(await fixtures())['runway-total-sender.json']
    expect(replies(f.rows, f.cfg, f.reply, f.variant, { total: true })).toEqual(total.replies)
    for (const marks of ARROW_MARKS) {
      const toks = replies(f.rows, f.cfg, f.reply, f.variant, { marks }).flatMap(r => tokenize(r.body))
      const kinds = new Set(toks.map(t => t.t))
      expect([marks, kinds.has(marks === 'arrow-circle' ? 'hue' : 'pool')]).toEqual([marks, true])
    }
  })
})
