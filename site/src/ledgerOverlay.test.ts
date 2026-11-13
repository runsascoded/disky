import { describe, expect, it } from 'vitest'
import { applyLedger } from './ledgerOverlay'
import { ownerIndex, type OwnerRow } from './owners'
import { unownedBytes, type TreeNode } from './types'

// The ownership ledger repaints the map's owner split (`owners`) client-side
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
  name: 'gs://', bytes: 1000, objects: 10, owners: [['will', 300], ['ryan', 400]], children: [
    { name: 'marin-us-central2', bytes: 600, objects: 6, owners: [['will', 300]], children: [
      { name: 'tomat', bytes: 400, objects: 4, owners: [['will', 100]], provenance: ['rule', null, 'gs://marin-us-central2/tomat/'], children: [
        { name: 'x', bytes: 250, objects: 2, owners: [['will', 100]] },
        { name: 'y', bytes: 150, objects: 2 },
      ] },
      { name: 'other', bytes: 200, objects: 2, owners: [['will', 200]] },
    ] },
    { name: 'marin-eu-west4', bytes: 400, objects: 4, owners: [['ryan', 400]], children: [
      { name: 'tomat', bytes: 400, objects: 4, owners: [['ryan', 400]] },
    ] },
  ],
})

describe('applyLedger', () => {
  it('moves assigned bytes out of unknown ownership and preserves the unknown remainder', () => {
    const tree: TreeNode = { name: 'gs://', bytes: 100, objects: 2, unknown_owner_bytes: 100, children: [
      { name: 'b', bytes: 100, objects: 2, unknown_owner_bytes: 100, children: [
        { name: 'assigned', bytes: 30, objects: 1, unknown_owner_bytes: 30 },
        { name: 'unknown', bytes: 70, objects: 1, unknown_owner_bytes: 70 },
      ] },
    ] }
    expect(applyLedger(tree, idx(row('gs://b/assigned/', 'alice', 1)), 'gs://')).toEqual({
      name: 'gs://', bytes: 100, objects: 2, unknown_owner_bytes: 70, owners: [['alice', 30]], children: [
        { name: 'b', bytes: 100, objects: 2, unknown_owner_bytes: 70, owners: [['alice', 30]], children: [
          { name: 'assigned', bytes: 30, objects: 1, owners: [['alice', 30]] },
          { name: 'unknown', bytes: 70, objects: 1, unknown_owner_bytes: 70 },
        ] },
      ],
    })
  })

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
      name: 'gs://', bytes: 1000, objects: 10, owners: [['ryan', 800], ['will', 200]], children: [
        { name: 'marin-us-central2', bytes: 600, objects: 6, owners: [['ryan', 400], ['will', 200]], children: [
          { name: 'tomat', bytes: 400, objects: 4, owners: [['ryan', 400]], children: [
            { name: 'x', bytes: 250, objects: 2, owners: [['ryan', 250]] },
            { name: 'y', bytes: 150, objects: 2, owners: [['ryan', 150]] },
          ] },
          { name: 'other', bytes: 200, objects: 2, owners: [['will', 200]] },
        ] },
        { name: 'marin-eu-west4', bytes: 400, objects: 4, owners: [['ryan', 400]], children: [
          { name: 'tomat', bytes: 400, objects: 4, owners: [['ryan', 400]] },
        ] },
      ],
    })
    expect(unownedBytes(out)).toBe(0)
  })

  it('a deeper assignment moves only its bytes; a release under an assignment restores the scan’s split', () => {
    const out = applyLedger(scan(), idx(
      row('gs://marin-us-central2/', 'ryan', 1),
      row('gs://marin-us-central2/tomat/x/', null, 2),
      row('gs://marin-eu-west4/tomat/', 'will', 1),
    ), 'gs://')
    expect(out).toEqual({
      name: 'gs://', bytes: 1000, objects: 10, owners: [['will', 500], ['ryan', 350]], children: [
        { name: 'marin-us-central2', bytes: 600, objects: 6, owners: [['ryan', 350], ['will', 100]], children: [
          { name: 'tomat', bytes: 400, objects: 4, owners: [['ryan', 150], ['will', 100]], children: [
            { name: 'x', bytes: 250, objects: 2, owners: [['will', 100]] },
            { name: 'y', bytes: 150, objects: 2, owners: [['ryan', 150]] },
          ] },
          { name: 'other', bytes: 200, objects: 2, owners: [['ryan', 200]] },
        ] },
        { name: 'marin-eu-west4', bytes: 400, objects: 4, owners: [['will', 400]], children: [
          { name: 'tomat', bytes: 400, objects: 4, owners: [['will', 400]] },
        ] },
      ],
    })
  })

  it('recency beats specificity: a newer ancestor assignment repaints an older deeper one', () => {
    const out = applyLedger(scan(), idx(
      row('gs://marin-us-central2/tomat/', 'will', 1),
      row('gs://marin-us-central2/', 'ryan', 2),
    ), 'gs://')
    expect(out.children![0].owners).toEqual([['ryan', 600]])
    expect(out.children![0].children![0].owners).toEqual([['ryan', 400]])
  })

  it('a fold (and bytes no drawn child accounts for) take the drawn parent’s assignee; a prefix inside an undrawn tile does not repaint it', () => {
    const t: TreeNode = {
      name: 'gs://', bytes: 1000, objects: 10, owners: [['will', 1000]], children: [
        { name: 'b', bytes: 1000, objects: 10, owners: [['will', 1000]], children: [
          { name: 'big', bytes: 500, objects: 5, owners: [['will', 500]] },
          { name: '(other)', bytes: 300, objects: 3, folded: 7, owners: [['will', 300]] },
        ] },
      ],
    }
    const out = applyLedger(t, idx(
      row('gs://b/', 'ryan', 1),
      row('gs://b/big/deep/', 'will', 2),
    ), 'gs://')
    expect(out).toEqual({
      name: 'gs://', bytes: 1000, objects: 10, owners: [['ryan', 1000]], children: [
        { name: 'b', bytes: 1000, objects: 10, owners: [['ryan', 1000]], children: [
          { name: 'big', bytes: 500, objects: 5, owners: [['ryan', 500]] },
          { name: '(other)', bytes: 300, objects: 3, folded: 7, owners: [['ryan', 300]] },
        ] },
      ],
    })
  })

  it('an assignee the scan knows under another spelling merges into that key', () => {
    const out = applyLedger(scan(), idx(row('gs://marin-us-central2/other/', 'Will', 1)), 'gs://', u => u.toLowerCase())
    expect(out.children![0].children![1].owners).toEqual([['will', 200]])
    const out2 = applyLedger(scan(), idx(row('gs://marin-us-central2/tomat/y/', 'Will', 1)), 'gs://', u => u.toLowerCase())
    expect(out2.children![0].owners).toEqual([['will', 450]])
    expect(out2.children![0].children![0].children![1].owners).toEqual([['will', 150]])
  })

  describe('with the rows’ sizes (`sizes`): every node exact over its whole subtree, drawn or not', () => {
    const sized = (...xs: [string, number, Record<string, number>][]) => new Map(xs.map(([k, b, us]) => [k, { b, us }]))

    it('a prefix inside an undrawn tile repaints it, and a fold takes its share of the rest', () => {
      const t: TreeNode = {
        name: 'gs://', bytes: 1000, objects: 10, owners: [['will', 1000]], children: [
          { name: 'b', bytes: 1000, objects: 10, owners: [['will', 1000]], children: [
            { name: 'big', bytes: 500, objects: 5, owners: [['will', 500]] },
            { name: '(other)', bytes: 300, objects: 3, folded: 7, owners: [['will', 300]] },
          ] },
        ],
      }
      const out = applyLedger(t, idx(
        row('gs://b/', 'ryan', 1),
        row('gs://b/big/deep/', 'will', 2),
      ), 'gs://', u => u, sized(['gs://b/', 1000, { will: 1000 }], ['gs://b/big/deep/', 100, { will: 100 }]))
      expect(out).toEqual({
        name: 'gs://', bytes: 1000, objects: 10, owners: [['ryan', 900], ['will', 100]], children: [
          { name: 'b', bytes: 1000, objects: 10, owners: [['ryan', 900], ['will', 100]], children: [
            { name: 'big', bytes: 500, objects: 5, owners: [['ryan', 400], ['will', 100]] },
            { name: '(other)', bytes: 300, objects: 3, folded: 7, owners: [['ryan', 300]] },
          ] },
        ],
      })
    })

    it('a release inside an assigned, childless tile restores the scan’s split of its subtree', () => {
      const t: TreeNode = {
        name: 'gs://', bytes: 1000, objects: 10, owners: [['will', 1000]], children: [
          { name: 'b', bytes: 1000, objects: 10, owners: [['will', 1000]] },
        ],
      }
      const out = applyLedger(t, idx(
        row('gs://b/', 'ryan', 1),
        row('gs://b/keep/', null, 2),
      ), 'gs://', u => u, sized(['gs://b/', 1000, { will: 1000 }], ['gs://b/keep/', 200, { will: 150 }]))
      expect(out).toEqual({
        name: 'gs://', bytes: 1000, objects: 10, owners: [['ryan', 800], ['will', 150]], children: [
          { name: 'b', bytes: 1000, objects: 10, owners: [['ryan', 800], ['will', 150]] },
        ],
      })
    })

    it('where the view draws every assigned prefix, the same tree as the drawn-nodes overlay', () => {
      const rows = [
        row('gs://marin-us-central2/', 'ryan', 1),
        row('gs://marin-us-central2/tomat/x/', null, 2),
        row('gs://marin-eu-west4/tomat/', 'will', 1),
      ]
      const sizes = sized(
        ['gs://marin-us-central2/', 600, { will: 300 }],
        ['gs://marin-us-central2/tomat/x/', 250, { will: 100 }],
        ['gs://marin-eu-west4/tomat/', 400, { ryan: 400 }],
      )
      expect(applyLedger(scan(), idx(...rows), 'gs://', u => u, sizes)).toEqual(applyLedger(scan(), idx(...rows), 'gs://'))
    })
  })
})
