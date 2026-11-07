import { expect, it } from 'vitest'
import { EventIds } from './event-ids'

function ulid(value: bigint): string {
  const alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
  let id = ''
  for (let i = 0; i < 26; i++) { id = alphabet[Number(value & 31n)] + id; value >>= 5n }
  return id
}

it('preserves every ULID bit, zero, the maximum, and duplicates across table growth', () => {
  const ids = [0n, ...Array.from({ length: 128 }, (_, i) => 1n << BigInt(i)), (1n << 128n) - 1n].map(ulid)
  const set = new EventIds()
  expect(ids.map(id => set.add(id))).toEqual(Array(130).fill(true))
  expect([...ids].reverse().map(id => set.has(id))).toEqual(Array(130).fill(true))
  expect(ids.map(id => set.add(id))).toEqual(Array(130).fill(false))
  expect(set.size).toBe(130)
})

it('compares all words when different ULIDs hash to the same slot', () => {
  const set = new EventIds(10)
  const ids = [1n, 1n << 32n, 1n << 64n, 1n << 96n].map(ulid)
  expect(ids.map(id => set.add(id))).toEqual([true, true, true, true])
  expect([set.has(ulid(2n)), ...ids.map(id => set.has(id)), set.size]).toEqual([false, true, true, true, true, 4])
})

it('keeps noncanonical and unfamiliar provider IDs exact without changing their spelling', () => {
  const set = new EventIds()
  const ids = ['', 'short', '80000000000000000000000000', '01k7f000000000000000000000', '0000000000000000000000000I', 'é0000000000000000000000000']
  expect(ids.map(id => set.add(id))).toEqual([true, true, true, true, true, true])
  expect(ids.map(id => set.has(id))).toEqual([true, true, true, true, true, true])
  expect(ids.map(id => set.add(id))).toEqual([false, false, false, false, false, false])
  expect([set.has('SHORT'), set.has('00000000000000000000000000'), set.size]).toEqual([false, false, 6])
})
