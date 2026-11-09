/** Local workerd parity adapter only: no deployed route, credentials or production routing. */
import { createExecutor, type Capsule, type KernelRequest } from './kernel.js'
import { readerMetadata, readerObjects } from '../functions/_lib/readerWorker.js'
import { filesFromObjects } from './stores.js'

interface Env { DB: D1Database; INDEX_R2: R2Bucket; CAPSULE: string }
let execute: ReturnType<typeof createExecutor> | undefined
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    if (!execute) {
      const capsule = JSON.parse(env.CAPSULE) as Capsule
      const objects = readerObjects(env)!
      execute = createExecutor(capsule, { objects, metadata: readerMetadata(env)!, files: filesFromObjects(objects), cache: caches.default })
    }
    try { return Response.json(await execute(await request.json() as KernelRequest, request.headers.get('x-fixture-cancel') === '1' ? AbortSignal.abort() : request.signal)) }
    catch (error) { return Response.json({ state: 'failed', name: (error as Error).name, message: (error as Error).message }, { status: 500 }) }
  },
}
