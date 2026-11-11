/** Portable I/O used by the existing map/diff reader. No authentication, bindings or background tasks. */
import type { Store } from '@rdub/file-tree'

export type ObjectRange = { offset: number; length?: number } | { suffix: number }
export interface ReadObject {
  size: number
  arrayBuffer(): Promise<ArrayBuffer>
  json<T>(): Promise<T>
}
export interface ReaderObjects {
  get(key: string, options?: { range?: ObjectRange }): Promise<ReadObject | null>
  head(key: string): Promise<{ size: number } | null>
  list(options: { prefix: string; cursor?: string }): Promise<{ objects: { key: string }[]; truncated: boolean; cursor?: string }>
}
export interface ReaderStatement {
  bind(...values: unknown[]): ReaderStatement
  first<T>(): Promise<T | null>
  all<T>(): Promise<{ results: T[] }>
  run(): Promise<unknown>
}
export interface ReaderMetadata {
  prepare(sql: string): ReaderStatement
  batch(statements: ReaderStatement[]): Promise<unknown>
}
/** The web response-cache operations the reader actually uses. */
export interface ReaderCache {
  match(key: RequestInfo | URL, options?: CacheQueryOptions): Promise<Response | undefined>
  put(key: RequestInfo | URL, response: Response): Promise<void>
  delete(key: RequestInfo | URL, options?: CacheQueryOptions): Promise<boolean>
}
export interface ReaderServices {
  objects: ReaderObjects
  metadata: ReaderMetadata
  files: Store
  cache: ReaderCache
}
/** A caller's execution limit is not evidence of a broken immutable store. */
export class ReaderStopped extends Error {}

const SERVICES = Symbol('reader services')
export const readerServices = (env: object): ReaderServices | undefined => (env as { [SERVICES]?: ReaderServices })[SERVICES]
export const withReaderServices = <E extends object>(env: E, services: ReaderServices): E => ({ ...env, [SERVICES]: services })

interface RuntimeContext {
  run<T>(context: { services: ReaderServices; execution?: ReaderExecution }, task: () => T): T
  getStore(): { services: ReaderServices; execution?: ReaderExecution } | undefined
}
export interface ReaderExecution { check(): void; read(): void; bytes(count: number): void; decoded(rows: number, ms: number): void }
let context: RuntimeContext | undefined
let loading: Promise<RuntimeContext> | undefined
/** Async context, rather than a mutable global: concurrent requests keep their own cache adapter. */
export async function runReader<T>(services: ReaderServices, task: () => Promise<T>, execution?: ReaderExecution): Promise<T> {
  const module = ['node', 'async_hooks'].join(':')
  const als = await (loading ??= import(/* @vite-ignore */ module).then(m => (context = new m.AsyncLocalStorage())))
  return als.run({ services, execution }, task)
}
export const readerCache = (): ReaderCache | undefined => context?.getStore()?.services.cache
export const readerExecution = (): ReaderExecution | undefined => context?.getStore()?.execution
