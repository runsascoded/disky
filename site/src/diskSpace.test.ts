import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { DiskSpace } from './DiskSpace'

describe('physical disk capacity', () => {
  it('omits the indicator when a snapshot has no measurement', () => {
    expect(renderToStaticMarkup(createElement(DiskSpace, {}))).toBe('')
  })

  it('uses physical capacity and free bytes, independent of the map totals, in the reader’s units (SI by default)', () => {
    const html = renderToStaticMarkup(createElement(DiskSpace, { space: {
      capacity: 1000 ** 3 * 100,
      used: 1000 ** 3 * 87.5,
      free: 1000 ** 3 * 12.5,
      device: 'disk3',
      captured_at: '2026-10-05T15:41:33Z',
    } }))
    expect(html.match(/<b>(.*?)<\/b>/)?.[1]).toBe('13 G free')
    expect(html.match(/class="dim">(.*?)<\/span>/)?.[1]).toBe('of 100 G')
    expect(html.match(/aria-valuetext="(.*?)"/)?.[1]).toBe('87.5% used, 13 G free')
    expect(html.match(/aria-valuenow="(.*?)"/)?.[1]).toBe('87.5')
    expect(html.match(/style="(.*?)"/)?.[1]).toBe('width:87.5%')
  })
})
