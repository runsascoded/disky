import { beforeEach, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { readAgePoint } from './ageStore'

const { ivGen, openInterval, readRows } = vi.hoisted(() => ({ ivGen: vi.fn(), openInterval: vi.fn(), readRows: vi.fn() }))
vi.mock('./index.js', () => ({ ivGen, openInterval, readRows }))

beforeEach(() => {
  vi.resetAllMocks()
  ivGen.mockResolvedValue('g')
  openInterval.mockResolvedValue('handle')
  readRows.mockResolvedValue([{ path: '1h/p=a%2Fb%20%21/1000', size: 0, n_files: 3 }])
})

it('uses an independent pointer and cache namespace, and decodes escaped paths and zero-byte cells', async () => {
  const env = { STORE_KEY: 'meta', INDEX_R2: {}, AGE_STORE_PREFIX: 'history/meta-age', INTERVAL_STORE_PREFIX: 'history/meta/interval-store', INTERVAL_STORE_GEN: 'primary-pin' } as Env
  expect(await readAgePoint(env, '2026-10-01', '1h', 'a/b !')).toEqual([
    { path: 'a/b !', depth: 2, binstart: 1000, b: 0, o: 3 },
  ])
  expect(openInterval.mock.calls).toEqual([[{
    ...env, STORE_KEY: 'meta-age', PATH_STORE: 'intervals', INTERVAL_STORE_PREFIX: 'history/meta-age/interval-store', INTERVAL_STORE_GEN: 'g',
  }, '2026-10-01', 'path']])
  expect(readRows.mock.calls).toEqual([['handle', 3, 3, '1h/p=a%2Fb%20%21/', '1h/p=a%2Fb%20%21/\uffff']])
})

it('falls back only for a store or scan without history', async () => {
  expect(await readAgePoint({} as Env, '2026-10-01', '1h', '')).toBeNull()
  expect(ivGen.mock.calls).toEqual([])
  const env = { INDEX_R2: {}, AGE_STORE_PREFIX: 'history/age' } as Env
  openInterval.mockResolvedValue(null)
  expect(await readAgePoint(env, '2026-10-01', '1h', '')).toBeNull()
  readRows.mockResolvedValue([])
  openInterval.mockResolvedValue('handle')
  expect(await readAgePoint(env, '2026-10-01', '1h', '')).toEqual([])
  openInterval.mockRejectedValue(new Error('broken run'))
  await expect(readAgePoint(env, '2026-10-01', '1h', '')).rejects.toThrow('broken run')
})
