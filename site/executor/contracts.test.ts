import { afterAll, beforeAll, expect, it } from 'vitest'
import { mkdir, mkdtemp, rm, writeFile, truncate } from 'node:fs/promises'
import { resolve } from 'node:path'
import { validBudgets } from './kernel'
import { readBounded } from './node'

let directory: string
beforeAll(async () => { await mkdir('../tmp', { recursive: true }); directory = await mkdtemp(resolve('../tmp/executor-contract-')) })
afterAll(async () => { await rm(directory, { recursive: true }) })

it('requires exactly the four named finite budgets, with integral byte/read limits', () => {
  expect([
    { gets: 1, bytes: 2, deadlineMs: 0.5, outputBytes: 3 },
    { gets: 1, bytes: 2, deadlineMs: 1, junk: 3 },
    { gets: 1, bytes: 2, outputBytes: 3, junk: 4 },
    { gets: 1, bytes: 2, deadlineMs: 1, outputBytes: 3, junk: 4 },
    { gets: 1, bytes: 2, deadlineMs: Infinity, outputBytes: 3 },
    { gets: 1, bytes: 2, deadlineMs: 1, outputBytes: '3' },
    { gets: 0.5, bytes: 2, deadlineMs: 1, outputBytes: 3 },
    null,
  ].map(validBudgets)).toEqual([true, false, false, false, false, false, false, false])
})

it('admits exact-cap and empty files, and rejects an oversized sparse file before allocating its contents', async () => {
  const file = `${directory}/input`
  await writeFile(file, Uint8Array.of(1, 2, 3))
  expect(await readBounded(file, 3)).toEqual(Uint8Array.of(1, 2, 3))
  await expect(readBounded(file, 2)).rejects.toThrow(new Error('capsule exceeds its input-byte cap'))
  await truncate(file, 256 * 1024 * 1024)
  await expect(readBounded(file, 16 * 1024 * 1024)).rejects.toThrow(new Error('capsule exceeds its input-byte cap'))
  await truncate(file, 0)
  expect(await readBounded(file, 0)).toEqual(new Uint8Array())
})
