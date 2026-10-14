import { expect, it } from 'vitest'
import { pickScanSelection } from './scanSelection'
import { decodeSel, encodeSel, DAY } from './scanSlug'

it('retains both pins when changing endpoints or brushing, including a latest-end pick', () => {
  const pins = decodeSel('26092812-26091700')
  expect([
    encodeSel(pickScanSelection(pins, '2026-10-13T1201', '2026-10-13T1201', undefined)),
    encodeSel(pickScanSelection(pins, '2026-09-30T1200', '2026-10-13T1201', 7 * DAY, '2026-09-20T0000')),
    encodeSel(pickScanSelection(decodeSel('-26091700'), '2026-10-13T1201', '2026-10-13T1201', undefined)),
    encodeSel(pickScanSelection(decodeSel('-7d'), '2026-10-13T1201', '2026-10-13T1201', 7 * DAY)),
  ]).toEqual(['2610131201-26091700', '2609301200-2609200000', '-26091700', '-7d'])
})

it('URL roundtrip preserves pinned versus floating modes', () => {
  expect(['26092812-26091700', '-26091700', '26092812-7d', '-7d'].map(value => decodeSel(encodeSel(decodeSel(value)))))
    .toEqual([
      { d: '2026-09-28T12', from: '2026-09-17T00' },
      { from: '2026-09-17T00' },
      { d: '2026-09-28T12', span: 7 * DAY },
      { span: 7 * DAY },
    ])
})
