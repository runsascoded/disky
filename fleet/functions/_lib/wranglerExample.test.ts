/** `wrangler.example.toml` names every setting the Functions read (`Env`'s fields), so a deploy copied from it misses
 *  none. `ASSETS` is Pages' own; `DEV_EMAIL` is local dev's (`.dev.vars`), never a Pages setting. */
import { describe, expect, it } from 'vitest'

interface Fs { readFile(p: string, enc: 'utf8'): Promise<string> }
const read = async (p: string) => (await (import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>)).readFile(p, 'utf8')

describe('wrangler.example.toml', () => {
  it('mentions every Env field', async () => {
    const src = await read('functions/_lib/env.ts')
    const body = /export interface Env \{([\s\S]*?)\n\}/.exec(src)![1]
    const fields = [...body.matchAll(/^ {2}([A-Z][A-Z0-9_]*)\??:/gm)].map(m => m[1]).filter(k => k !== 'ASSETS' && k !== 'DEV_EMAIL')
    const toml = await read('wrangler.example.toml')
    expect(fields.filter(k => !new RegExp(`\\b${k}\\b`).test(toml))).toEqual([])
    expect(fields).toEqual([
      'SESSION_SECRET', 'STAFF_DOMAIN', 'OA_LOGIN_ISSUER', 'OA_LOGIN_CLIENT_ID', 'OA_LOGIN_CLIENT_SECRET', 'FLEET_JSON', 'FLEET_RUNS',
      'FLEET_JOB_TOKEN', 'DISKY_SLACK_BOT_TOKEN', 'SLACK_FLEET_CHANNEL', 'SLACK_FLEET_TS', 'FLEET_KV',
    ])
  })
})
