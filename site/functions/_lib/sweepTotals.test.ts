import { NotFoundError } from '@rdub/file-tree'
import { describe, expect, it, vi } from 'vitest'
import { PROGRESS_BYTES, READ_MS, runTotalsOf, totalsReaderOf } from './sweepTotals'

const enc = (doc: unknown) => ({ bytes: new TextEncoder().encode(typeof doc === 'string' ? doc : JSON.stringify(doc)) })
const store = (files: Record<string, unknown>) => {
  const asked: [string, { offset: number; length: number }][] = []
  const get = async (key: string, range: { offset: number; length: number }) => {
    asked.push([key, range])
    if (!(key in files)) throw new NotFoundError(key)
    return enc(files[key])
  }
  return { asked, get }
}
const progress = (bucket: string, deletes: number, bytes: number, extra: Record<string, number> = {}) => ({
  bucket, mode: 'deleted', decisions: { delete: deletes, ...extra }, delete_bytes: bytes, done: false,
})
const DIR = 'sweep/runs/j'

describe('runTotalsOf: what a run with no end record wrote', () => {
  it('sums each bucket\'s `progress/<bucket>.json`: a lower bound; a bucket with none counts nothing', async () => {
    const { asked, get } = store({
      [`${DIR}/progress/b1.json`]: progress('b1', 5, 50, { skipped_gone: 2, delete_failed: 9 }),
      [`${DIR}/progress/b2.json`]: progress('b2', 3, 30, { skipped_overwritten: 1 }),
    })
    expect(await runTotalsOf('j', 'b1, b2,b3', get)).toEqual({ deleted_objects: 8, deleted_bytes: 80, skipped_gone: 2, skipped_overwritten: 1, drift_dirs: 0, exact: false })
    const range = { offset: 0, length: PROGRESS_BYTES }
    expect(asked).toEqual([
      [`${DIR}/deleted-summary.json`, range],
      [`${DIR}/progress/b1.json`, range], [`${DIR}/progress/b2.json`, range], [`${DIR}/progress/b3.json`, range],
    ])
  })
  it('nothing written: zeros, still a lower bound', async () => {
    expect(await runTotalsOf('j', 'b1', store({}).get)).toEqual({ deleted_objects: 0, deleted_bytes: 0, skipped_gone: 0, skipped_overwritten: 0, drift_dirs: 0, exact: false })
  })
  it('prefers `deleted-summary.json`, which the executor writes when its counts are final: exact', async () => {
    const { asked, get } = store({
      [`${DIR}/deleted-summary.json`]: { for_real: true, buckets: {
        b1: { decisions: { delete: 5, skipped_gone: 2 }, delete_bytes: 50, drift_dirs: [{}, {}] },
        b2: { decisions: { delete: 3, skipped_overwritten: 1 }, delete_bytes: 30 },
      } },
    })
    expect(await runTotalsOf('j', 'b1,b2', get)).toEqual({ deleted_objects: 8, deleted_bytes: 80, skipped_gone: 2, skipped_overwritten: 1, drift_dirs: 2, exact: true })
    expect(asked.map(([key]) => key)).toEqual([`${DIR}/deleted-summary.json`])
  })
  it('a summary too long to read whole falls to the progress files', async () => {
    const { get } = store({ [`${DIR}/deleted-summary.json`]: '{"for_real": true, "buck', [`${DIR}/progress/b1.json`]: progress('b1', 5, 50) })
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    expect(await runTotalsOf('j', 'b1', get)).toEqual({ deleted_objects: 5, deleted_bytes: 50, skipped_gone: 0, skipped_overwritten: 0, drift_dirs: 0, exact: false })
    log.mockRestore()
  })
  it('a row with no buckets recorded (an image before they were) names them from `plan-summary.json`', async () => {
    const { get } = store({ [`${DIR}/plan-summary.json`]: { buckets: { b1: {}, b2: {} } }, [`${DIR}/progress/b2.json`]: progress('b2', 3, 30) })
    expect(await runTotalsOf('j', null, get)).toEqual({ deleted_objects: 3, deleted_bytes: 30, skipped_gone: 0, skipped_overwritten: 0, drift_dirs: 0, exact: false })
  })
  it('a bucket\'s file it can\'t read throws: not a lower bound of anything', async () => {
    await expect(runTotalsOf('j', 'b1', store({ [`${DIR}/progress/b1.json`]: '{"truncated' }).get)).rejects.toThrow(SyntaxError)
  })
})

describe('totalsReaderOf: never throws, never slow', () => {
  it('a failed read is null', async () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    expect(await totalsReaderOf(async () => { throw new Error('store down') })('j', 'b1')).toBeNull()
    expect(log.mock.calls).toEqual([['totals of j: store down']])
    log.mockRestore()
  })
  it('a read that hangs is null after its time box', async () => {
    vi.useFakeTimers()
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    const read = totalsReaderOf(() => new Promise(() => {}))('j', 'b1')
    await vi.advanceTimersByTimeAsync(READ_MS)
    expect(await read).toBeNull()
    expect(log.mock.calls).toEqual([[`totals of j: read took over ${READ_MS} ms`]])
    log.mockRestore()
    vi.useRealTimers()
  })
})
