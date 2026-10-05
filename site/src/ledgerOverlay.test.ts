import { describe, expect, it } from 'vitest'
import { applyLedger } from './ledgerOverlay'
import { ownerIndex, type OwnerRow } from './owners'
import { unclaimedBytes, type TreeNode } from './types'

// The ownership ledger repaints the map's owner split (`us`) client-side
// (specs/done/path-agnostic-serving.md §2.3): an assigned prefix moves its bytes
// from their scan owner(s) to the assignee, in every drawn node at, under,
// and above it.

let aid = 0
const row = (prefix: string, owner: string | null, ts: number): OwnerRow =>
  ({ prefix, owner, ts, who: 'admin', memo: null, action_id: ++aid })
const idx = (...rows: OwnerRow[]) => ownerIndex({ owners: rows })

// Two buckets; the user's 2026-10-02 report in miniature: `tomat` under
// central2 is mostly unowned in the scan, eu-west4's is already theirs.
const scan = (): TreeNode => ({
  n: 'gs://', b: 1000, o: 10, us: [['will', 300], ['ryan', 400]], c: [
    { n: 'marin-us-central2', b: 600, o: 6, us: [['will', 300]], c: [
      { n: 'tomat', b: 400, o: 4, us: [['will', 100]], pv: ['rule', null, 'gs://marin-us-central2/tomat/'], c: [
        { n: 'x', b: 250, o: 2, us: [['will', 100]] },
        { n: 'y', b: 150, o: 2 },
      ] },
      { n: 'other', b: 200, o: 2, us: [['will', 200]] },
    ] },
    { n: 'marin-eu-west4', b: 400, o: 4, us: [['ryan', 400]], c: [
      { n: 'tomat', b: 400, o: 4, us: [['ryan', 400]] },
    ] },
  ],
})

describe('applyLedger', () => {
  it('no live assignment: the scan tree, as is (same object)', () => {
    const t = scan()
    expect(applyLedger(t, idx(), 'gs://')).toBe(t)
    expect(applyLedger(t, idx(row('gs://marin-us-central2/tomat/', null, 1)), 'gs://')).toBe(t)
  })

  it('an assigned prefix is wholly the assignee’s, and its ancestors’ splits (the legend) follow', () => {
    const out = applyLedger(scan(), idx(
      row('gs://marin-us-central2/tomat/', 'ryan', 1),
      row('gs://marin-eu-west4/tomat/', 'ryan', 1),
    ), 'gs://')
    expect(out).toEqual({
      n: 'gs://', b: 1000, o: 10, us: [['ryan', 800], ['will', 200]], c: [
        { n: 'marin-us-central2', b: 600, o: 6, us: [['ryan', 400], ['will', 200]], c: [
          { n: 'tomat', b: 400, o: 4, us: [['ryan', 400]], c: [
            { n: 'x', b: 250, o: 2, us: [['ryan', 250]] },
            { n: 'y', b: 150, o: 2, us: [['ryan', 150]] },
          ] },
          { n: 'other', b: 200, o: 2, us: [['will', 200]] },
        ] },
        { n: 'marin-eu-west4', b: 400, o: 4, us: [['ryan', 400]], c: [
          { n: 'tomat', b: 400, o: 4, us: [['ryan', 400]] },
        ] },
      ],
    })
    expect(unclaimedBytes(out)).toBe(0)
  })

  it('a deeper assignment moves only its bytes; a release under an assignment restores the scan’s split', () => {
    const out = applyLedger(scan(), idx(
      row('gs://marin-us-central2/', 'ryan', 1),
      row('gs://marin-us-central2/tomat/x/', null, 2),
      row('gs://marin-eu-west4/tomat/', 'will', 1),
    ), 'gs://')
    expect(out).toEqual({
      n: 'gs://', b: 1000, o: 10, us: [['will', 500], ['ryan', 350]], c: [
        { n: 'marin-us-central2', b: 600, o: 6, us: [['ryan', 350], ['will', 100]], c: [
          { n: 'tomat', b: 400, o: 4, us: [['ryan', 150], ['will', 100]], c: [
            { n: 'x', b: 250, o: 2, us: [['will', 100]] },
            { n: 'y', b: 150, o: 2, us: [['ryan', 150]] },
          ] },
          { n: 'other', b: 200, o: 2, us: [['ryan', 200]] },
        ] },
        { n: 'marin-eu-west4', b: 400, o: 4, us: [['will', 400]], c: [
          { n: 'tomat', b: 400, o: 4, us: [['will', 400]] },
        ] },
      ],
    })
  })

  it('recency beats specificity: a newer ancestor assignment repaints an older deeper one', () => {
    const out = applyLedger(scan(), idx(
      row('gs://marin-us-central2/tomat/', 'will', 1),
      row('gs://marin-us-central2/', 'ryan', 2),
    ), 'gs://')
    expect(out.c![0].us).toEqual([['ryan', 600]])
    expect(out.c![0].c![0].us).toEqual([['ryan', 400]])
  })

  it('a fold (and bytes no drawn child accounts for) take the drawn parent’s assignee; a prefix inside an undrawn tile does not repaint it', () => {
    const t: TreeNode = {
      n: 'gs://', b: 1000, o: 10, us: [['will', 1000]], c: [
        { n: 'b', b: 1000, o: 10, us: [['will', 1000]], c: [
          { n: 'big', b: 500, o: 5, us: [['will', 500]] },
          { n: '(other)', b: 300, o: 3, f: 7, us: [['will', 300]] },
        ] },
      ],
    }
    const out = applyLedger(t, idx(
      row('gs://b/', 'ryan', 1),
      row('gs://b/big/deep/', 'will', 2),
    ), 'gs://')
    expect(out).toEqual({
      n: 'gs://', b: 1000, o: 10, us: [['ryan', 1000]], c: [
        { n: 'b', b: 1000, o: 10, us: [['ryan', 1000]], c: [
          { n: 'big', b: 500, o: 5, us: [['ryan', 500]] },
          { n: '(other)', b: 300, o: 3, f: 7, us: [['ryan', 300]] },
        ] },
      ],
    })
  })

  it('an assignee the scan knows under another spelling merges into that key', () => {
    const out = applyLedger(scan(), idx(row('gs://marin-us-central2/other/', 'Will', 1)), 'gs://', u => u.toLowerCase())
    expect(out.c![0].c![1].us).toEqual([['will', 200]])
    const out2 = applyLedger(scan(), idx(row('gs://marin-us-central2/tomat/y/', 'Will', 1)), 'gs://', u => u.toLowerCase())
    expect(out2.c![0].us).toEqual([['will', 450]])
    expect(out2.c![0].c![0].c![1].us).toEqual([['will', 150]])
  })
})
