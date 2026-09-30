/** The `/api/*` Functions end to end over the fixture "bucket": the same
 *  request/response contract the Flask server implements (`ui/src/api.ts`). */
import { beforeEach, describe, expect, it } from 'vitest'
import { join } from 'node:path'
import { dirBucket } from './fakeR2'
import { resetScanCache } from '../manifests'
import type { Env } from '../env'
import { onRequestGet as scans } from '../../functions/api/scans'
import { onRequestGet as scan } from '../../functions/api/scan'
import { onRequestGet as history } from '../../functions/api/scans/history'
import { onRequestGet as capabilities } from '../../functions/api/capabilities'
import { onRequest as fallback } from '../../functions/api/[[path]]'
import { onRequest as middleware } from '../../functions/_middleware'

const PREFIX = 'scans/'
const env: Env = { SCANS: dirBucket(join(__dirname, 'fixtures'), PREFIX), SCANS_PREFIX: PREFIX }
const T = '2026-01-02T03:04:05'
const T0 = 1_700_000_000

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = async (fn: (ctx: any) => Promise<Response>, path: string) => {
  const res = await fn({ request: new Request(`http://x${path}`), env, params: {} })
  return { status: res.status, body: await res.json() }
}

beforeEach(resetScanCache)

const SCAN = { id: 1, path: '/fixture', time: T, blob: 'fixture.parquet', error_count: null, error_paths: null, size: 3600, n_children: 4, n_desc: 11, mtime: T0 }

describe('/api/scans', () => {
  it('lists the newest scan per path from the manifests', async () => {
    expect(await call(scans, '/api/scans')).toEqual({ status: 200, body: [SCAN] })
  })
})

describe('/api/scan', () => {
  it('serves the scan root: children at depth 1, rows to the requested depth', async () => {
    const { status, body } = await call(scan, '/api/scan?uri=/fixture&depth=2&max_rows=2000')
    expect(status).toBe(200)
    expect(body.root).toEqual({ path: '.', parent: null, uri: '/fixture', kind: 'dir', size: 3600, mtime: T0, n_desc: 11, n_children: 4, depth: 0 })
    expect(body.children.map((r: { path: string }) => r.path)).toEqual(['a', 'b', 't1', 't2'])
    expect(body.children[0]).toEqual({ path: 'a', parent: '.', uri: '/fixture/a', kind: 'dir', size: 1000, mtime: T0 + 3600, n_desc: 5, n_children: 3, depth: 1, scanned: true, scan_time: T })
    expect(body.rows.map((r: { path: string }) => r.path)).toEqual(['a', 'b', 't1', 't2', 'a/c', 'a/f1', 'a/f2', 'b/h1', 'b/h2'])
    expect({ ...body, root: 0, children: 0, rows: 0 }).toEqual({
      root: 0, children: 0, rows: 0,
      time: T, scan_path: '/fixture', scan_status: 'full', error_count: null, error_paths: null, collapsed_rows: [],
    })
  })

  it('rebases a subdirectory of the scan to `.`', async () => {
    const { status, body } = await call(scan, '/api/scan?uri=/fixture/a&depth=1')
    expect(status).toBe(200)
    expect(body.root).toEqual({ path: '.', parent: null, uri: '/fixture/a', kind: 'dir', size: 1000, mtime: T0 + 3600, n_desc: 5, n_children: 3, depth: 0 })
    expect(body.rows).toEqual([
      { path: 'c', parent: '.', uri: '/fixture/a/c', kind: 'dir', size: 700, mtime: T0 + 5 * 3600, n_desc: 2, n_children: 2, depth: 1 },
      { path: 'f1', parent: '.', uri: '/fixture/a/f1', kind: 'file', size: 100, mtime: T0 + 6 * 3600, n_desc: 0, n_children: 0, depth: 1 },
      { path: 'f2', parent: '.', uri: '/fixture/a/f2', kind: 'file', size: 200, mtime: T0 + 7 * 3600, n_desc: 0, n_children: 0, depth: 1 },
    ])
    expect(body.children.map((r: { path: string; scanned: boolean }) => [r.path, r.scanned])).toEqual([['c', true], ['f1', true], ['f2', true]])
  })

  it('rebases parents two levels down', async () => {
    const { body } = await call(scan, '/api/scan?uri=/fixture/a&depth=2')
    expect(body.rows.map((r: { path: string; parent: string; depth: number }) => [r.path, r.parent, r.depth])).toEqual([
      ['c', '.', 1], ['f1', '.', 1], ['f2', '.', 1], ['c/g1', 'c', 2], ['c/g2', 'c', 2],
    ])
  })

  it('caps rows at max_rows by size, keeping ancestors', async () => {
    const { body } = await call(scan, '/api/scan?uri=/fixture&depth=3&max_rows=2')
    // Biggest two of the 11 non-root rows are b (1100) and a (1000): no extra ancestors needed.
    expect(body.rows.map((r: { path: string }) => r.path)).toEqual(['b', 'a'])
    const deep = await call(scan, '/api/scan?uri=/fixture/a&depth=2&max_rows=1')
    // Biggest under a is c (700) — then g2 would need c; with max_rows=1 only c.
    expect(deep.body.rows.map((r: { path: string }) => r.path)).toEqual(['c'])
  })

  it('answers the Flask error shapes', async () => {
    expect(await call(scan, '/api/scan?uri=/nope')).toEqual({ status: 404, body: { error: 'No scan found for path', uri: '/nope' } })
    expect(await call(scan, '/api/scan?uri=/fixture/missing')).toEqual({ status: 404, body: { error: 'URI not found in scan', uri: '/fixture/missing', scan_path: '/fixture' } })
    expect(await call(scan, '/api/scan?uri=/elsewhere&scan_id=1')).toEqual({ status: 400, body: { error: 'Scan 1 does not cover path /elsewhere' } })
  })
})

describe('/api/scans/history', () => {
  it('lists scans of the uri and its ancestors, with subpath stats for ancestors', async () => {
    expect(await call(history, '/api/scans/history?uri=/fixture')).toEqual({
      status: 200, body: [{ id: 1, path: '/fixture', time: T, size: 3600, n_children: 4, n_desc: 11, scan_path: '/fixture' }],
    })
    expect(await call(history, '/api/scans/history?uri=/fixture/a/c')).toEqual({
      status: 200, body: [{ id: 1, path: '/fixture', time: T, size: 700, n_children: 2, n_desc: 2, scan_path: '/fixture' }],
    })
    expect(await call(history, '/api/scans/history?uri=/fixture/nope')).toEqual({ status: 200, body: [] })
  })
})

describe('capabilities + fallback', () => {
  it('declares the static deployment and refuses everything else', async () => {
    const caps = await call(capabilities, '/api/capabilities')
    expect(caps.status).toBe(200)
    expect(Object.entries(caps.body).filter(([, v]) => v)).toEqual([['static', true], ['compare', true], ['stageDelete', true], ['deleteApproval', 'staged'], ['auth', true]])
    expect(await call(fallback, '/api/histogram?uri=/x')).toEqual({
      status: 501, body: { error: 'not available in the static (cloud) deployment', path: '/api/histogram' },
    })
  })
})

describe('open demo (PUBLIC_OPEN)', () => {
  const openEnv: Env = { ...env, PUBLIC_OPEN: '1' }
  const gated = async (req: Request) =>
    middleware({ request: req, env: openEnv, next: async () => new Response('served', { status: 200 }) } as never)

  it('reports auth: false so the UI shows no wall', async () => {
    const res = await capabilities({ request: new Request('http://x/api/capabilities'), env: openEnv, params: {} } as never)
    expect(Object.entries(await res.json()).filter(([, v]) => v)).toEqual([['static', true], ['compare', true], ['deleteApproval', 'staged']])
  })

  it('serves every /api/* route without a session (no gate, no DB)', async () => {
    const res = await gated(new Request('http://x/api/scans'))
    expect({ status: res.status, body: await res.text() }).toEqual({ status: 200, body: 'served' })
  })
})
