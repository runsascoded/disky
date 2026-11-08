/** A local execution contract around the production map/diff kernel. It is not an authenticated HTTP API. */
import { buildDiff, buildView, type DiffOpts, type ViewOpts } from '../functions/_lib/view.js'
import { plain } from '../functions/_lib/querySyntax.js'
import { QueryError } from '../functions/_lib/queryAst.js'
import { ivRefusal, TooWide } from '../functions/_lib/index.js'
import { parseQuery } from '../functions/_lib/pathQuery.js'
import { FilterRejected, indexedOnly, pathTerms, rejectQuery, rejectScope } from '../functions/_lib/indexedOnly.js'
import { reqOf, withReq } from '../functions/_lib/shared.js'
import { ReaderStopped, readerExecution, runReader, withReaderServices, type ReaderServices } from '../functions/_lib/readerRuntime.js'

/** Explicitly bounded, deployment-owned reader knobs; no credentials or platform handles. */
export interface KernelConfig {
  PATH_STORE: 'intervals'
  INTERVAL_STORE_GEN: string
  INTERVAL_STORE_PREFIX?: string
  INTERVAL_STORE_REV?: string
  ROOT_LABEL?: string
  FILTER_STATIC?: string
  FILTER_STATIC_HEAVY?: string
  FILTER_STATIC_ANCHORS?: string
  FILTER_INDEXED_ONLY?: string
  STATIC_GEN?: string
  NAME_SUMMARY_STATIC?: string
  SEARCH_PATH_TERMS?: string
  SERVED_R2?: string
}
export interface Capsule {
  version: 1
  id: string
  revision: { store: string; generation: string; manifest: string; ledger: string; deletion: string }
  dates: string[]
  config: KernelConfig
}
type Controls = Omit<ViewOpts, 'date' | 'query' | 'trace' | 'phase2Gate' | 'meetFloor' | 'nestForce' | 'classes'> & { query?: string; classes?: ('1' | '2' | '3' | '4')[] }
type DiffControls = Omit<DiffOpts, keyof ViewOpts> & Controls
export type KernelRequest = { version: 1; capsule: string; budgets: Budgets } & (
  | { kind: 'map'; options: Controls & { date: string } }
  | { kind: 'diff'; options: DiffControls & { from: string; to: string } }
)
export interface Budgets { deadlineMs: number; gets: number; bytes: number; outputBytes: number }
export class ExecutionLimit extends ReaderStopped {
  constructor(readonly code: 'deadline' | 'gets' | 'bytes' | 'output' | 'cancelled') { super(`executor ${code} limit`); this.name = 'ExecutionLimit' }
}

// Existing kernel memos assume one deployment per isolate. Enforce that rather than pretending every memo is
// namespaced already. New capsules need a new ordinary process / Worker isolate; concurrent calls share one capsule.
let bound: { capsule: Capsule; services: ReaderServices } | undefined
export function createExecutor(input: Capsule, services: ReaderServices) {
  const capsule = structuredClone(input)
  Object.freeze(capsule.config)
  Object.freeze(capsule.revision)
  Object.freeze(capsule.dates)
  Object.freeze(capsule)
  if (capsule.version !== 1 || capsule.config.INTERVAL_STORE_GEN !== capsule.revision.generation) throw new Error('invalid capsule generation')
  if (bound) throw new Error('executor already bound: use a new process for another capsule')
  bound = { capsule, services }
  const read = async <T>(task: () => Promise<T>): Promise<T> => { readerExecution()?.read(); const value = await task(); readerExecution()?.check(); return value }
  const io: ReaderServices = {
      ...services,
      objects: {
        head: key => read(() => services.objects.head(key)),
        list: options => read(() => services.objects.list(options)),
        get: async (key, options) => {
          const object = await read(() => services.objects.get(key, options))
          if (!object) return null
          const bytes = async () => { readerExecution()?.check(); const buf = await object.arrayBuffer(); readerExecution()?.bytes(buf.byteLength); return buf }
          return { size: object.size, arrayBuffer: bytes, json: async <T>() => JSON.parse(new TextDecoder().decode(await bytes())) as T }
        },
      },
      files: {
        list: (prefix, options) => read(() => services.files.list(prefix, options)),
        get: async (key, range) => {
          const result = await read(() => services.files.get(key, range))
          readerExecution()?.bytes(result.bytes.byteLength); return result
        },
      },
    }
  return async (request: KernelRequest, signal?: AbortSignal) => {
    const start = performance.now()
    const counters = { gets: 0, bytes: 0, decodedRows: 0, decodeMs: 0 }
    const trace: { name: string; value: number; description?: string }[] = []
    const identity = { capsule: capsule.id, revision: capsule.revision }
    if (request.version !== 1 || request.capsule !== capsule.id) return { ...identity, state: 'unavailable' as const, code: 'capsule-mismatch' }
    const dates = request.kind === 'map' ? [request.options.date] : [request.options.from, request.options.to]
    if (dates.some(d => !capsule.dates.includes(d))) return { ...identity, state: 'unavailable' as const, code: 'scan-not-in-capsule' }
    const b = request.budgets
    if (Object.values(b).length !== 4 || Object.values(b).some(n => !Number.isFinite(n) || n < 0)) throw new Error('invalid execution budgets')
    const check = () => {
      if (signal?.aborted) throw new ExecutionLimit('cancelled')
      if (performance.now() - start >= b.deadlineMs) throw new ExecutionLimit('deadline')
      if (counters.gets > b.gets) throw new ExecutionLimit('gets')
      if (counters.bytes > b.bytes) throw new ExecutionLimit('bytes')
    }
    const env = withReq(withReaderServices({ ...capsule.config, SERVED_R2: '1' }, io), signal)
    try {
      check()
      const body = await runReader(io, async () => {
        const refusal = indexedOnly(capsule.config) ? rejectQuery(request.options.query, pathTerms(capsule.config)) ?? (request.options.query ? rejectScope(!!(request.options.lens || request.options.classes?.length)) : null) : null
        if (refusal) throw new FilterRejected(refusal)
        const query = request.options.query ? parseQuery(request.options.query, plain) ?? undefined : undefined
        const options = { ...request.options, query, classes: request.options.classes ? new Set(request.options.classes) : undefined, trace: (name: string, value: number, description?: string) => trace.push({ name, value, ...(description ? { description } : {}) }) }
        return request.kind === 'map' ? buildView(env, options as ViewOpts) : buildDiff(env, options as DiffOpts)
      }, { check, read: () => { counters.gets++; check() }, bytes: count => { counters.bytes += count; check() }, decoded: (rows, ms) => { counters.decodedRows += rows; counters.decodeMs += ms; check() } })
      check()
      const serializationStart = performance.now()
      const outputBytes = new TextEncoder().encode(JSON.stringify(body)).byteLength
      const serializationMs = performance.now() - serializationStart
      if (outputBytes > b.outputBytes) throw new ExecutionLimit('output')
      // Preserve the entire kernel body, including remainders, coverage and its own partial-answer flags.
      return { ...identity, state: 'answered' as const, body, telemetry: { ...counters, outputBytes, serializationMs, elapsedMs: performance.now() - start, trace } }
    } catch (error) {
      if (error instanceof ExecutionLimit) return { ...identity, state: 'exhausted' as const, code: error.code }
      if (error instanceof QueryError) return { ...identity, state: 'unsupported' as const, code: error.code, message: error.message }
      if (error instanceof TooWide) return { ...identity, state: 'exhausted' as const, code: 'reader-width', message: error.message }
      const refusal = ivRefusal(error)
      if (refusal) return { ...identity, state: 'unavailable' as const, code: 'store-unreadable', message: await refusal.text(), retryAfter: refusal.headers.get('retry-after') }
      if (error instanceof FilterRejected) return { ...identity, state: 'unsupported' as const, refusal: error.reject }
      throw error
    } finally {
      reqOf(env)?.kill()
    }
  }
}
