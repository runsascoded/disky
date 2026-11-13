import type { Peer } from './hosts'

const FLAGS = ['owners', 'staging', 'meta'] as const
const KEYS = new Set<string>(['label', 'prod', 'dev', ...FLAGS])

/** The deployment's sibling deployments, from its `PEERS` var (wrangler `[vars]`, read at build time by vite.config.ts):
 * a JSON array of `Peer`s (`label`, `prod`; optional `dev`, `owners`, `staging`, `meta`). The list may name this
 * deployment too: the site skips its own prod host. Unset or empty: no peers. Anything else malformed throws, naming the
 * entry, so a bad var fails the build rather than the page. */
export function parsePeers(raw: string | undefined): Peer[] {
  const text = (raw ?? '').trim()
  if (!text) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    throw new Error(`PEERS: not JSON (${(e as Error).message})`)
  }
  if (!Array.isArray(parsed)) throw new Error('PEERS: expected a JSON array of peers')
  return parsed.map((p, i): Peer => {
    if (typeof p !== 'object' || p === null || Array.isArray(p)) throw new Error(`PEERS[${i}]: expected an object`)
    const o = p as Record<string, unknown>
    const unknown = Object.keys(o).filter(k => !KEYS.has(k)).sort()
    if (unknown.length) throw new Error(`PEERS[${i}]: unknown keys ${unknown.join(', ')}`)
    for (const k of ['label', 'prod'] as const) {
      if (typeof o[k] !== 'string' || !o[k]) throw new Error(`PEERS[${i}].${k}: expected a non-empty string`)
    }
    const peer: Peer = { label: o.label as string, prod: o.prod as string }
    if (o.dev !== undefined) {
      if (typeof o.dev !== 'string' || !o.dev) throw new Error(`PEERS[${i}].dev: expected a non-empty string`)
      peer.dev = o.dev
    }
    for (const k of FLAGS) {
      if (o[k] === undefined) continue
      if (typeof o[k] !== 'boolean') throw new Error(`PEERS[${i}].${k}: expected a boolean`)
      peer[k] = o[k] as boolean
    }
    return peer
  })
}
