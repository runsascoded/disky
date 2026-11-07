import { describe, expect, it } from 'vitest'
import { canvasWidth } from './canvas'

describe('canvasWidth', () => {
  it('snaps up to the nearest width the scan job warms', () => {
    expect([390, 512, 513, 960, 1024, 1280, 1281, 1440, 1600, 1792, 1900, 1920].map(canvasWidth)).toEqual(
      [512, 512, 1280, 1280, 1280, 1280, 1536, 1536, 1792, 1792, 1920, 1920],
    )
  })
  it('past the widest warmed width, keeps the 128-px step', () => {
    expect([1921, 2048, 2560, 3000].map(canvasWidth)).toEqual([2048, 2048, 2560, 3072])
  })
})
