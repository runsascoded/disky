/** PNG export helpers (`src/exportImage.ts`) for `<Treemap exportable>`. The
 *  canvas compositing itself needs a real 2d context (jsdom has none), so these
 *  cover the surrounding logic: the default filename, the clipboard/download
 *  seams, and the no-title short-circuit. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  canvasToPngBlob,
  composeExport,
  copyPng,
  defaultExportFilename,
  downloadPng,
} from '../src/exportImage'

const AT = new Date(2026, 8, 16, 12, 1) // 2026-09-16 12:01 (month is 0-indexed)

describe('defaultExportFilename', () => {
  it('is <view-basename>-<YYYYMMDD-HHMM>.png', () => {
    expect(defaultExportFilename('marin-us-east-02a', AT)).toBe('marin-us-east-02a-20260916-1201.png')
  })
  it('takes the last path segment, dropping a trailing slash', () => {
    expect(defaultExportFilename('/var/lib/data/', AT)).toBe('data-20260916-1201.png')
  })
  it('maps filename-unsafe characters to hyphens and trims them', () => {
    expect(defaultExportFilename('my scans (2026)!', AT)).toBe('my-scans-2026-20260916-1201.png')
  })
  it('falls back to "treemap" when the label has no usable segment', () => {
    expect(defaultExportFilename('/', AT)).toBe('treemap-20260916-1201.png')
  })
  it('zero-pads month, day, hour, and minute', () => {
    expect(defaultExportFilename('x', new Date(2026, 0, 3, 4, 5))).toBe('x-20260103-0405.png')
  })
})

describe('composeExport', () => {
  it('returns the source canvas unchanged when there is no title', () => {
    const src = document.createElement('canvas')
    expect(composeExport(src, { title: null, bg: '#000', ink: '#fff', dpr: 2 })).toBe(src)
  })
})

describe('copyPng', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('writes one image/png ClipboardItem and returns true', async () => {
    const items: Record<string, Blob>[] = []
    class FakeClipboardItem {
      constructor(data: Record<string, Blob>) { items.push(data) }
    }
    const write = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('ClipboardItem', FakeClipboardItem)
    vi.stubGlobal('navigator', { clipboard: { write } })
    const blob = new Blob(['x'], { type: 'image/png' })

    expect(await copyPng(blob)).toBe(true)
    expect(write).toHaveBeenCalledTimes(1)
    expect(write.mock.calls[0][0]).toHaveLength(1)
    expect(items).toEqual([{ 'image/png': blob }])
  })

  it('returns false (no throw) when ClipboardItem is unavailable', async () => {
    const write = vi.fn()
    vi.stubGlobal('ClipboardItem', undefined)
    vi.stubGlobal('navigator', { clipboard: { write } })

    expect(await copyPng(new Blob(['x']))).toBe(false)
    expect(write).not.toHaveBeenCalled()
  })

  it('returns false when clipboard.write rejects (e.g. denied permission)', async () => {
    class FakeClipboardItem { constructor(_: Record<string, Blob>) {} }
    const write = vi.fn().mockRejectedValue(new Error('denied'))
    vi.stubGlobal('ClipboardItem', FakeClipboardItem)
    vi.stubGlobal('navigator', { clipboard: { write } })

    expect(await copyPng(new Blob(['x']))).toBe(false)
  })
})

describe('downloadPng', () => {
  afterEach(() => {
    vi.runAllTimers() // fire the deferred revoke while URL is still stubbed
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('clicks an <a download> with the object URL, then cleans up', () => {
    vi.useFakeTimers()
    const createObjectURL = vi.fn().mockReturnValue('blob:fake-url')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    let clicked: { href: string; download: string } | null = null
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked = { href: this.href, download: this.download }
    })
    const blob = new Blob(['x'], { type: 'image/png' })

    downloadPng(blob, 'scan-20260916-1201.png')

    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(clicked).toEqual({ href: 'blob:fake-url', download: 'scan-20260916-1201.png' })
    // No orphan anchor left in the document.
    expect(document.querySelectorAll('a[download]')).toHaveLength(0)
    // The object URL is revoked once the click has had a chance to start.
    vi.runAllTimers()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:fake-url')
  })

  it('appends .png when the filename lacks the extension', () => {
    vi.useFakeTimers()
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:x', revokeObjectURL: vi.fn() })
    let download = ''
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      download = this.download
    })

    downloadPng(new Blob(['x']), 'my-scan')

    expect(download).toBe('my-scan.png')
  })
})

describe('canvasToPngBlob', () => {
  it('rejects when the browser cannot encode a blob (jsdom toBlob → null)', async () => {
    const cv = document.createElement('canvas')
    cv.toBlob = (cb: BlobCallback) => cb(null)
    await expect(canvasToPngBlob(cv)).rejects.toThrow('canvas.toBlob returned null')
  })
})
