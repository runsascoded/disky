import { describe, expect, it } from 'vitest'
import { pathTree } from './pathTree'

describe('pathTree: prefixes as an indented tree', () => {
  it('shared parent once, chain collapsed, sibling leaves packed', () => {
    expect(pathTree(['d1280', 'd1024', 'd768', 'd512'].map(d => `gs://marin-us-central1/grug/tied_experts/${d}/`))).toEqual([
      'marin-us-central1/grug/tied_experts/',
      '  d1024/ d1280/ d512/ d768/',
    ])
  })

  it('mixed depths across buckets; a staged dir with staged children keeps both', () => {
    expect(pathTree([
      'gs://b1/ego-dex/',
      'gs://b1/SpatialVID/',
      'gs://b2/ckpt/sft/run-a/',
      'gs://b2/ckpt/sft/run-b/',
      'gs://b2/ckpt/other/',
      'gs://b2/raw/x/',
      'gs://b2/raw/x/y/',
    ])).toEqual([
      'b1/',
      '  SpatialVID/ ego-dex/',
      'b2/',
      '  ckpt/',
      '    other/',
      '    sft/',
      '      run-a/ run-b/',
      '  raw/x/',
      '    y/',
    ])
  })

  it('wraps packed leaves at `width`; cuts past `maxLines` with the staged count left', () => {
    const many = Array.from({ length: 12 }, (_, i) => `gs://b/canary/run-${String(i).padStart(2, '0')}/`)
    expect(pathTree(many, { width: 30 })).toEqual([
      'b/canary/',
      '  run-00/ run-01/ run-02/',
      '  run-03/ run-04/ run-05/',
      '  run-06/ run-07/ run-08/',
      '  run-09/ run-10/ run-11/',
    ])
    expect(pathTree(many, { width: 30, maxLines: 3 })).toEqual([
      'b/canary/',
      '  run-00/ run-01/ run-02/',
      '… 9 more',
    ])
  })
})
