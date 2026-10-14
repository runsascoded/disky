import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { DiskSpaceDiff, diskDiffScan, spaceChanges } from './DiskSpaceDiff'
import type { DiskSpace } from './types'

const before: DiskSpace = { capacity: 500, used: 480, free: 20, device: 'disk3', captured_at: '2026-10-12T10:08Z', vm_used: 32 }
const after: DiskSpace = { ...before, used: 440, free: 60, captured_at: '2026-10-12T10:39Z', vm_used: 0 }

describe('whole-disk space comparison', () => {
  it('reports the physical change separately from indexed files, including zero swap allocation', () => {
    expect(spaceChanges(before, after)).toEqual([
      { label: 'Free space', before: 20, after: 60, change: 40 },
      { label: 'Disk used', before: 480, after: 440, change: -40 },
      { label: 'VM (swap volume)', before: 32, after: 0, change: -32 },
    ])
  })
  it('does not invent a VM measurement when an older snapshot lacks it', () => {
    const { vm_used: _, ...old } = before
    expect(spaceChanges(old, after)).toEqual([
      { label: 'Free space', before: 20, after: 60, change: 40 },
      { label: 'Disk used', before: 480, after: 440, change: -40 },
    ])
  })
  it('requires two readings of the same container and capacity', () => {
    expect([
      spaceChanges(undefined, after),
      spaceChanges(before, undefined),
      spaceChanges(before, { ...after, device: 'disk4' }),
      spaceChanges(before, { ...after, capacity: 1000 }),
    ]).toEqual([[], [], [], []])
  })
})

describe('a store without disk readings (no `meta.disk_space`) is unchanged', () => {
  it('fetches no earlier meta and renders nothing', () => {
    expect([diskDiffScan(undefined, '2026-10-11'), diskDiffScan(after, '2026-10-11'), diskDiffScan(after, null)]).toEqual([null, '2026-10-11', null])
    expect([
      renderToStaticMarkup(createElement(DiskSpaceDiff, {})),
      renderToStaticMarkup(createElement(DiskSpaceDiff, { after })),
    ]).toEqual(['', ''])
  })
})
