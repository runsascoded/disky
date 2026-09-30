/** Hybrid chunk following in `/api/scan` (`storage/hybrid.py` splits a big
 *  depth-1 dir into its own blob, stamping the parent with `child_scan_id`).
 *  The static Function must (a) serve the subtree when you view *into* a chunk
 *  and (b) splice a chunk-stub child's breakdown into the parent's treemap —
 *  the two Flask behaviors (`server.py`) that were "no chunk following yet".
 *  Fixtures: `fixtures-chunked/gen.py`. */
import { beforeEach, describe, expect, it } from 'vitest'
import { join } from 'node:path'
import { dirBucket } from './fakeR2'
import { resetScanCache } from '../manifests'
import type { Env } from '../env'
import { onRequestGet as scan } from '../../functions/api/scan'
import { onRequestGet as scans } from '../../functions/api/scans'

const PREFIX = 'scans/'
const env: Env = { SCANS: dirBucket(join(__dirname, 'fixtures-chunked'), PREFIX), SCANS_PREFIX: PREFIX }
const T = '2026-01-02T03:04:05'
const T0 = 1_700_000_000

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = async (fn: (ctx: any) => Promise<Response>, path: string) => {
  const res = await fn({ request: new Request(`http://x${path}`), env, params: {} })
  return { status: res.status, body: await res.json() }
}

beforeEach(resetScanCache)

describe('/api/scan chunk following', () => {
  it('lists the single chunked scan (chunk blobs carry no manifest)', async () => {
    const { body } = await call(scans, '/api/scans')
    expect(body.map((s: { path: string; blob: string }) => [s.path, s.blob])).toEqual([['/chunked', 'chunked.parquet']])
  })

  it('serves a subtree that lives in a chunk when viewed into (the empty-drill fix)', async () => {
    // `big` is a chunk stub in the main blob; its children live in chunk-big.parquet.
    const { status, body } = await call(scan, '/api/scan?uri=/chunked/big&depth=2')
    expect(status).toBe(200)
    expect(body.root).toEqual({ path: '.', parent: null, uri: '/chunked/big', kind: 'dir', size: 2000, mtime: T0, n_desc: 3, n_children: 2, depth: 0 })
    expect(body.children).toEqual([
      { path: 'p', parent: '.', uri: '/chunked/big/p', kind: 'dir', size: 1200, mtime: T0 + 3600, n_desc: 1, n_children: 1, depth: 1, scanned: true, scan_time: T },
      { path: 'q', parent: '.', uri: '/chunked/big/q', kind: 'file', size: 800, mtime: T0 + 2 * 3600, n_desc: 0, n_children: 0, depth: 1, scanned: true, scan_time: T },
    ])
    expect(body.rows).toEqual([
      { path: 'p', parent: '.', uri: '/chunked/big/p', kind: 'dir', size: 1200, mtime: T0 + 3600, n_desc: 1, n_children: 1, depth: 1 },
      { path: 'q', parent: '.', uri: '/chunked/big/q', kind: 'file', size: 800, mtime: T0 + 2 * 3600, n_desc: 0, n_children: 0, depth: 1 },
      { path: 'p/r', parent: 'p', uri: '/chunked/big/p/r', kind: 'file', size: 1200, mtime: T0 + 3 * 3600, n_desc: 0, n_children: 0, depth: 2 },
    ])
  })

  it('splices a chunk-stub child’s top-level breakdown into the parent view', async () => {
    const { status, body } = await call(scan, '/api/scan?uri=/chunked&depth=2')
    expect(status).toBe(200)
    expect(body.children.map((r: { path: string }) => r.path)).toEqual(['big', 'small'])
    // `big`'s children (from its chunk) appear as depth-2 rows under `big`.
    const underBig = body.rows.filter((r: { parent: string }) => r.parent === 'big')
    expect(underBig).toEqual([
      { path: 'big/p', parent: 'big', uri: '/chunked/big/p', kind: 'dir', size: 1200, mtime: T0 + 3600, n_desc: 1, n_children: 1, depth: 2 },
      { path: 'big/q', parent: 'big', uri: '/chunked/big/q', kind: 'file', size: 800, mtime: T0 + 2 * 3600, n_desc: 0, n_children: 0, depth: 2 },
    ])
    // `small` stays served from the main blob (in-blob children unaffected).
    expect(body.rows.filter((r: { parent: string }) => r.parent === 'small').map((r: { path: string }) => r.path)).toEqual(['small/x', 'small/y'])
  })
})
