import { expect, it } from 'vitest'
import { COALESCE_S } from './stagedSlack'
import { BATCH_GAP_S, batchLeads, coalesceBatches, idRanges } from './batchGroups'

const b = (id: number, created_by: string, created_ts: number, note: string | null = null) => ({ id, created_by, created_ts, note })
const ids = (gs: { id: number }[][]) => gs.map(g => g.map(x => x.id))

it('groups by the same window as the Slack thread coalesces replies', () => {
  expect(BATCH_GAP_S).toBe(COALESCE_S)
})

it("one stager's row-by-row clicks are one group; a gap past the window starts another", () => {
  const t = 1_000_000
  // David's 10/13 session: #30–#40 over 42 s, re-stages #41–#56 a minute later; then a 15m01s gap.
  const bs = [
    ...[30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40].map((id, i) => b(id, 'david', t + 4 * i)),
    ...[41, 42, 43].map(id => b(id, 'David', t + 106)),
    b(57, 'david', t + 106 + BATCH_GAP_S),
    b(58, 'david', t + 107 + 2 * BATCH_GAP_S),
  ]
  expect(ids(coalesceBatches(bs))).toEqual([
    [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 57],
    [58],
  ])
})

it("splits by stager and by note; another stager's batches in between don't split a group", () => {
  const bs = [b(1, 'ann', 100), b(2, 'bob', 110), b(3, 'ann', 120), b(4, 'ann', 130, 'tmp sweep'), b(5, 'ann', 140, 'tmp sweep'), b(6, 'bob', 2000)]
  const groups = coalesceBatches(bs)
  expect([ids(groups), [...batchLeads(groups)]]).toEqual([
    [[1, 3], [2], [4, 5], [6]],
    [[1, 1], [3, 1], [2, 2], [4, 4], [5, 4], [6, 6]],
  ])
})

it('input order does not matter (the API lists newest first); equal times order by id', () => {
  expect(ids(coalesceBatches([b(3, 'a', 50), b(2, 'a', 50), b(1, 'a', 10)]))).toEqual([[1, 2, 3]])
})

it('ids as runs', () => {
  expect([idRanges([30]), idRanges([32, 30, 31, 35, 37, 36]), idRanges([])]).toEqual(['#30', '#30–32, #35–37', ''])
})
