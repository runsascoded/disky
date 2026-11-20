/** The fleet card's SVG, byte for byte, against its goldens (`tests/golden/`; regenerate with `node scripts/golden.mjs`
 *  after a deliberate change, and look at `--png`'s output before committing them). */
import { describe, expect, it } from 'vitest'
import { fleetSvg, fmtTick, mix, PANELS } from './card'
import { readFleet } from './fleet'
import { ANSWERS, FLEET, fleetEnv, NOW, stubFetch } from './testFleet'

// Node's fs, loaded dynamically: the Functions typecheck has no Node types.
interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const golden = async (name: string): Promise<string> =>
  (await (import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>)).readFile(`tests/golden/${name}.svg`, 'utf8')

const deployments = FLEET.map(({ name, color }) => ({ name, color }))

describe('the fleet card', () => {
  it('full, dark and light: the test fleet\'s week, byte for byte', async () => {
    const doc = await readFleet(fleetEnv(), NOW, stubFetch(ANSWERS))
    expect(fleetSvg({ tier: 'full', theme: 'dark', deployments, doc, now: doc.now }) + '\n').toBe(await golden('card-full-dark'))
    expect(fleetSvg({ tier: 'full', theme: 'light', deployments, doc, now: doc.now }) + '\n').toBe(await golden('card-full-light'))
  })
  it('anonymous: the frame, names and colours, no numbers', async () => {
    const svg = fleetSvg({ tier: 'anon', theme: 'dark', deployments, now: NOW })
    expect(svg + '\n').toBe(await golden('card-anon-dark'))
    // Nothing on it is a size, a duration or a cost: every text is a name, a title, a date or the prompt.
    const texts = [...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1])
    expect(texts).toEqual([
      'disky fleet', 'sign in for the numbers', 'gcs', 'cw', 'hf', 'r2',
      '/meta store size', '11/1', '11/2', '11/3', '11/4', '11/5', '11/6', '11/7',
      'objects (primary store)', '11/1', '11/2', '11/3', '11/4', '11/5', '11/6', '11/7', 'sign in to see the fleet',
      'scan duration (h)', '11/1', '11/2', '11/3', '11/4', '11/5', '11/6', '11/7',
      'cost per day ($, complete UTC days)', '11/1', '11/2', '11/3', '11/4', '11/5', '11/6', '11/7',
    ])
  })
  it('split: an image per panel under the full header, taller than its slot, all on the stacked card\'s x axis', async () => {
    const doc = await readFleet(fleetEnv(), NOW, stubFetch(ANSWERS))
    const input = { tier: 'full', theme: 'dark', deployments, doc, now: doc.now } as const
    // a panel's title (12.5 px, bold) and its plot area (the panel colour's rect: x, width, height)
    const titles = (svg: string) => [...svg.matchAll(/font-size="12\.5" font-weight="600"[^>]*>([^<]*)</g)].map(m => m[1])
    const areas = (svg: string) => [...svg.matchAll(/<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)" height="(\d+)" fill="#232322"\/>/g)].map(m => m.slice(1).join(' '))
    const whole = fleetSvg(input)
    const split = PANELS.map((_, only) => fleetSvg({ ...input, only }))
    const x = areas(whole)[0].split(' ').slice(0, 2).join(' ')
    expect([areas(whole), split.map(titles), split.map(areas)]).toEqual([
      [`${x} 118`, `${x} 100`, `${x} 118`, `${x} 100`],
      [['/meta store size'], ['objects (primary store)'], ['scan duration (h)'], ['cost per day ($, complete UTC days)']],
      [[`${x} 201`], [`${x} 170`], [`${x} 201`], [`${x} 170`]],
    ])
  })
  it('round byte ticks and colour mixing', () => {
    expect([fmtTick(1e9), fmtTick(1e8), fmtTick(2e10), fmtTick(5e11)]).toEqual(['1 GB', '100 MB', '20 GB', '500 GB'])
    expect([mix('#FFD21E', '#000000', 0), mix('#ffd21e', '#000000', 0.5), mix('#000000', '#ffffff', 1)]).toEqual(['#ffd21e', '#80690f', '#ffffff'])
  })
})
