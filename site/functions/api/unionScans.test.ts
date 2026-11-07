import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'

import { unionDeployment } from '../_lib/testUnion'
import { onRequestGet as unionScans } from './union-scans'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/union-scans`: the scan picker's sources on a union view — each member's mark and its indexed scans at
// their instants; `{union: null}` wherever a request reads one store. The deployment: `_lib/testUnion.ts`.

describe('D1', () => {
  let deployment: (views?: Record<string, unknown>) => Env
  beforeAll(async () => { deployment = await unionDeployment() })
  const get = async (qs: string, views?: Record<string, unknown>) => {
    const r = await unionScans({ request: new Request(`http://localhost/api/union-scans?${qs}`), env: deployment(views) } as never)
    return { status: r.status, body: await r.json() }
  }

  it('the main view: each member’s label (its `ROOT_LABEL` by default), configured icon, scans newest first', async () => {
    expect(await get('', { all: { union: ['primary', 'meta'], main: true, sources: { meta: { label: 'Index', icon: '/icons/meta.svg' } } } })).toEqual({
      status: 200,
      body: { union: { view: 'all', sources: [
        { store: 'primary', label: 'Fleet', scans: [
          { id: '2026-10-03T0001', at: '2026-10-03T00:01:00.000Z' },
          { id: '2026-10-01T0001', at: '2026-10-01T00:01:00.000Z' },
          { id: '2026-09-30T0001', at: '2026-09-30T00:01:00.000Z' },
        ] },
        { store: 'meta', label: 'Index', icon: '/icons/meta.svg', scans: [
          { id: '2026-10-02T0001', at: '2026-10-02T00:01:00.000Z' },
          { id: '2026-10-01T0001', at: '2026-10-01T00:01:00.000Z' },
        ] },
      ] } },
    })
  })

  it('an icon site path the Functions would answer (not under `/icons/`, `_routes.json`) is warned of; a URL is not', async () => {
    const views = { all: { union: ['primary', 'meta'], main: true, sources: { primary: { icon: 'https://example.com/p.png' }, meta: { icon: '/meta.svg' } } } }
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const r = await get('', views)
    const w = "STORES_JSON.all.sources.meta.icon '/meta.svg': not excluded from the Functions (public/_routes.json), so it is served the SPA's index.html; put it under /icons/"
    expect([r.status, (r.body as { union: { warnings?: string[] } }).union.warnings, warn.mock.calls]).toEqual([200, [w], [[`union-scans: ${w}`]]])
    warn.mockRestore()
  })

  it('a store (or a deployment without a view) reads one store: no union', async () => {
    expect([await get('store=primary'), await get('store=meta'), await get('', {})]).toEqual([
      { status: 200, body: { union: null } },
      { status: 200, body: { union: null } },
      { status: 200, body: { union: null } },
    ])
  })
})
