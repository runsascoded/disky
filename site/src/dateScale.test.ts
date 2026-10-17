import { expect, it } from 'vitest'
import { dateAxis, dateInk } from './dateScale'

const r3 = (x: number) => +x.toFixed(3)

it('log spreads the newest days and compresses the old tail; `day` inverts `t`', () => {
  const ax = dateAxis({ min: 20_000, max: 20_100 }, 'log')
  expect([20_000, 20_050, 20_090, 20_099, 20_100].map(d => r3(ax.t(d)))).toEqual([0, 0.148, 0.48, 0.85, 1])
  expect([0, 0.25, 0.5, 0.75, 1].map(t => Math.round(ax.day(t)))).toEqual([20_000, 20_069, 20_091, 20_098, 20_100])
})

it('lin is even in calendar time', () => {
  const ax = dateAxis({ min: 20_000, max: 20_100 }, 'lin')
  expect([20_000, 20_050, 20_090, 20_100].map(d => r3(ax.t(d)))).toEqual([0, 0.5, 0.9, 1])
  expect([0, 0.5, 1].map(t => ax.day(t))).toEqual([20_000, 20_050, 20_100])
})

it('rank spaces days by their bytes, however far apart in time; ends clamp', () => {
  const ax = dateAxis({ min: 0, max: 100 }, 'rank', [[0, 5], [1, 5], [2, 5], [100, 5]])
  expect(ax.scale).toBe('rank')
  expect([-5, 0, 1, 2, 50, 100, 120].map(d => r3(ax.t(d)))).toEqual([0, 0, 0.333, 0.667, 0.83, 1, 1])
  expect([0, 0.5, 5 / 6, 1].map(t => r3(ax.day(t)))).toEqual([0, 1.5, 51, 100])
  // Bytes, not cells: a heavy oldest day pushes the next one most of the way up the gradient (each
  // knot sits mid-way through its day's bytes: .45, .925, .975 of the CDF); a heavy middle day
  // sits mid-gradient.
  const heavy = dateAxis({ min: 0, max: 100 }, 'rank', [[0, 90], [50, 5], [100, 5]])
  expect([0, 50, 100].map(d => r3(heavy.t(d)))).toEqual([0, 0.905, 1])
  const mid = dateAxis({ min: 0, max: 100 }, 'rank', [[0, 10], [50, 80], [100, 10]])
  expect([0, 50, 100].map(d => r3(mid.t(d)))).toEqual([0, 0.5, 1])
})

it('rank with fewer than two distinct days in range is lin', () => {
  const ax = dateAxis({ min: 0, max: 100 }, 'rank', [[40, 5], [40, 7], [200, 1]])
  expect(ax.scale).toBe('lin')
  expect([0, 40, 100].map(d => ax.t(d))).toEqual([0, 0.4, 1])
})

it("a table's ink: its own rows' range, newest = the gradient's end", () => {
  expect(dateInk([], 'log')(5)).toBe('rgb(253,231,37)')
  const ink = dateInk([[0, 1], [100, 1]], 'lin')
  expect([0, 50, 100].map(ink)).toEqual(['rgb(68,1,84)', 'rgb(33,145,140)', 'rgb(253,231,37)'])
})
