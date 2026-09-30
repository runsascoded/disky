/** The edge R2 CFN executor (spec `staged-delete.md` CP7) over an in-memory R2:
 *  binding resolution, key collection (object + prefix), the threshold cutoff,
 *  and bulk delete. */
import { describe, expect, it } from 'vitest'
import type { Env } from '../env'
import { collectKeys, deleteKeys, planR2Deletion, r2Binding } from '../r2exec'

/** Just enough of `R2Bucket`: an in-memory key→size store that records deletes. */
class FakeR2 {
  objects = new Map<string, number>()
  deleted: string[] = []
  seed(entries: Record<string, number>) {
    for (const [k, v] of Object.entries(entries)) this.objects.set(k, v)
    return this
  }
  async head(key: string) {
    return this.objects.has(key) ? { key, size: this.objects.get(key)! } : null
  }
  async list({ prefix = '' }: { prefix?: string; cursor?: string; limit?: number }) {
    const objects = [...this.objects.entries()]
      .filter(([k]) => k.startsWith(prefix))
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([key, size]) => ({ key, size }))
    return { objects, truncated: false, cursor: undefined, delimitedPrefixes: [] }
  }
  async delete(keys: string | string[]) {
    for (const k of Array.isArray(keys) ? keys : [keys]) {
      this.objects.delete(k)
      this.deleted.push(k)
    }
  }
}

const bucket = () => new FakeR2() as unknown as R2Bucket
const envWith = (buckets: Record<string, R2Bucket>): Env => buckets as unknown as Env

describe('r2Binding', () => {
  it('resolves an r2 uri to its `R2_<bucket>` binding, sanitizing the name', () => {
    const jc = bucket()
    const env = envWith({ R2_jc_taxes: jc })
    const b = r2Binding(env, 'r2://jc-taxes/2024/f.pdf')
    expect(b?.bucket).toBe(jc)
    expect(b?.key).toBe('2024/f.pdf')
  })

  it('is null for an unbound bucket or a non-r2 uri', () => {
    const env = envWith({ R2_ctbk: bucket() })
    expect(r2Binding(env, 'r2://other/x')).toBeNull()
    expect(r2Binding(env, 's3://ctbk/x')).toBeNull()
    expect(r2Binding(env, 'r2://ctbk')).toBeNull() // no key
  })
})

describe('collectKeys', () => {
  it('collects the exact object plus everything under the prefix', async () => {
    const b = new FakeR2().seed({ 'logs': 0, 'logs/a': 10, 'logs/b/c': 20, 'other': 5 }) as unknown as R2Bucket
    const { keys, over } = await collectKeys(b, 'logs', 100)
    expect(over).toBe(false)
    expect(keys.map(k => k.key).sort()).toEqual(['logs', 'logs/a', 'logs/b/c'])
    expect(keys.reduce((a, k) => a + k.size, 0)).toBe(30)
  })

  it('collects a lone file (no prefix children)', async () => {
    const b = new FakeR2().seed({ 'backup.tar': 1000 }) as unknown as R2Bucket
    const { keys } = await collectKeys(b, 'backup.tar', 100)
    expect(keys).toEqual([{ key: 'backup.tar', size: 1000 }])
  })

  it('reports `over` once it passes the limit', async () => {
    const b = new FakeR2().seed({ 'd/1': 1, 'd/2': 1, 'd/3': 1 }) as unknown as R2Bucket
    const { over } = await collectKeys(b, 'd', 2)
    expect(over).toBe(true)
  })
})

describe('planR2Deletion', () => {
  it('plans every uri when all resolve and fit the budget', async () => {
    const ctbk = new FakeR2().seed({ 'a/1': 5, 'a/2': 7 }) as unknown as R2Bucket
    const env = envWith({ R2_ctbk: ctbk })
    const plan = await planR2Deletion(env, ['r2://ctbk/a'], 100)
    expect(plan?.map(p => [p.uri, p.keys.length])).toEqual([['r2://ctbk/a', 2]])
  })

  it('is null (→ drainer) when a uri is not a bound bucket', async () => {
    const env = envWith({ R2_ctbk: new FakeR2().seed({ 'a/1': 1 }) as unknown as R2Bucket })
    expect(await planR2Deletion(env, ['r2://ctbk/a', 's3://x/y'], 100)).toBeNull()
  })

  it('is null (→ drainer) when the cumulative scope exceeds the budget', async () => {
    const ctbk = new FakeR2().seed({ 'a/1': 1, 'a/2': 1, 'a/3': 1 }) as unknown as R2Bucket
    const env = envWith({ R2_ctbk: ctbk })
    expect(await planR2Deletion(env, ['r2://ctbk/a'], 2)).toBeNull()
  })
})

describe('deleteKeys', () => {
  it('deletes the keys and returns the bytes freed', async () => {
    const b = new FakeR2().seed({ 'x': 100, 'y': 200 })
    const freed = await deleteKeys(b as unknown as R2Bucket, [{ key: 'x', size: 100 }, { key: 'y', size: 200 }])
    expect(freed).toBe(300)
    expect(b.deleted.sort()).toEqual(['x', 'y'])
    expect(b.objects.size).toBe(0)
  })
})
