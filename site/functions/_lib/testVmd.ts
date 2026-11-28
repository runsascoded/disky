/** A fake `vmd` for tests (specs/vm-mode.md §4): swapped in as `VMD.fetch`, it checks each request's tenant and
 * signature as `vmd` does, answers `GET /v1/status` and `POST /v1/view` from the test's callbacks, and signs its replies
 * (unless `unsigned`). Nothing outside `*.test.ts` imports this file. */
import { signRequest, signResponse } from './vmAdapter.js'
import { VMD, type VmdStatus } from './vmServe.js'

export const VM_ENV = { VMD_URL: 'https://vmd.test', VM_TENANT: 't1', VM_KEY: 'k' }

export interface FakeVmd {
  /** `<method> <path>` per authenticated request. */
  calls: string[]
  /** Each view request's body, parsed. */
  views: Record<string, unknown>[]
  /** `VMD.fetch` as it was. */
  restore: () => void
}

export function fakeVmd(o: {
  status?: () => VmdStatus | Response
  view?: (body: Record<string, unknown>) => { status: number; body: unknown }
  unsigned?: boolean
}): FakeVmd {
  const real = VMD.fetch
  const calls: string[] = []
  const views: Record<string, unknown>[] = []
  VMD.fetch = async (r: Request) => {
    const path = new URL(r.url).pathname
    const body = await r.text()
    const ts = r.headers.get('x-disky-ts') ?? ''
    const want = await signRequest(VM_ENV.VM_KEY, VM_ENV.VM_TENANT, r.method, path, body, Number(ts))
    if (r.headers.get('x-disky-tenant') !== VM_ENV.VM_TENANT || want['x-disky-sig'] !== r.headers.get('x-disky-sig')) {
      return new Response('{"error":"bad-signature","message":"signature mismatch"}', { status: 401 })
    }
    calls.push(`${r.method} ${path}`)
    let status = 200
    let out: unknown
    if (path === '/v1/status' && o.status) {
      const s = o.status()
      if (s instanceof Response) return s
      out = s
    } else if (path === '/v1/view' && o.view) {
      const parsed = JSON.parse(body) as Record<string, unknown>
      views.push(parsed)
      ;({ status, body: out } = o.view(parsed))
    } else {
      return new Response(JSON.stringify({ error: 'not-found', message: path }), { status: 404 })
    }
    const text = JSON.stringify(out)
    return new Response(text, { status, headers: o.unsigned ? {} : { 'x-disky-sig': await signResponse(VM_ENV.VM_KEY, VM_ENV.VM_TENANT, ts, text) } })
  }
  return { calls, views, restore: () => { VMD.fetch = real } }
}
