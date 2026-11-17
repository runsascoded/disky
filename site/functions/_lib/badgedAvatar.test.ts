// A staged-deletion sender's badged face: which badge each verb carries, the SVG drawn (fold or ring), and the PNG
// published as a public avatar (rendered by the vendored resvg, as on Workers), or the plain face when it can't be made.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { badgedAvatarUrl, badgedSvg, badgeOf, resetBadgedAvatars } from './badgedAvatar'
import { identiconPng, readAvatar } from './publicAvatar'
import { sqliteD1 } from './testD1'

const ORIGIN = 'https://site.example.org'
const FACE = 'https://img/ann.png'

afterEach(() => { vi.unstubAllGlobals(); resetBadgedAvatars() })

it('each verb carries its badge; a dispatch that isn\'t real is a dry run; other verbs none', () => {
  expect(['staged', 'added', 'dispatched', 'finished', 'failed', 'unstaged', 'rejected', 'undone', 'noted'].map(v => badgeOf(v, 'real')))
    .toEqual(['staged', 'staged', 'dispatched', 'finished', 'failed', 'undone', 'undone', 'undone', null])
  expect([badgeOf('dispatched', 'dry'), badgeOf('dispatched'), badgeOf('dispatched', null)]).toEqual(['dry', 'dispatched', 'dispatched'])
})

describe('badgedSvg', () => {
  it('fold: the face, a corner folded down in the badge\'s color, its glyph (white) on it', () => {
    expect(badgedSvg('data:image/png;base64,AA', 'finished')).toBe(
      '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 100 100">' +
      '<image href="data:image/png;base64,AA" width="100" height="100" preserveAspectRatio="xMidYMid slice"/>' +
      '<path d="M100 44V100H44z" fill="#2e9e62"/><path d="M100 44L44 100" stroke="#000" stroke-opacity="0.18" stroke-width="2"/>' +
      '<g transform="translate(70 70) scale(1)"><path d="M5 12.5l4.5 4.5L19 7" fill="none" stroke="#fff" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"/></g></svg>',
    )
  })
  it('ring: the face framed in the badge\'s color, a filled glyph on a corner chip', () => {
    expect(badgedSvg('data:image/png;base64,AA', 'dispatched', 'ring', 128)).toBe(
      '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 100 100">' +
      '<image href="data:image/png;base64,AA" width="100" height="100" preserveAspectRatio="xMidYMid slice"/>' +
      '<rect x="3.5" y="3.5" width="93" height="93" rx="19" fill="none" stroke="#ec6a1f" stroke-width="7"/><circle cx="79" cy="79" r="18" fill="#ec6a1f"/>' +
      '<g transform="translate(69 69) scale(0.8333333333333334)"><path d="M13.5 2.5L5.5 13.5h6l-1 8 8-11h-6z" fill="#fff" stroke="none"/></g></svg>',
    )
  })
})

describe('D1', () => {
  /** A fetch that serves `bytes` at the face's URL (and counts the gets), 404 elsewhere. */
  function serve(bytes: Uint8Array | null) {
    const gets: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: string) => {
      gets.push(input)
      return input === FACE && bytes ? new Response(bytes, { headers: { 'content-type': 'image/png' } }) : new Response('', { status: 404 })
    }))
    return gets
  }
  /** A PNG's width and height (its IHDR). */
  const size = (png: Uint8Array) => { const v = new DataView(png.buffer, png.byteOffset); return [v.getUint32(16), v.getUint32(20)] }

  it('renders the badged face once, publishes it at /a/<sha256>.png, and reuses it', async () => {
    const { db } = await sqliteD1()
    const gets = serve(identiconPng(new Uint8Array([1, 2, 3, 4]), 96))
    const who = { email: 'ann@example.org', image: FACE }
    const url = await badgedAvatarUrl({}, db, ORIGIN, who, 'staged')
    const again = await badgedAvatarUrl({}, db, ORIGIN, who, 'staged')
    const m = /^https:\/\/site\.example\.org(\/a\/[0-9a-f]{64}\.png)$/.exec(url ?? '')
    const stored = m ? await readAvatar(db, m[1]) : null
    expect([!!m, again, gets, stored?.type, stored ? size(stored.bytes) : null]).toEqual([true, url, [FACE], 'image/png', [256, 256]])
  })

  it('no photo: the actor\'s identicon is badged when there is a SESSION_SECRET; without one, or on http, nothing', async () => {
    const { db } = await sqliteD1()
    const gets = serve(null)
    const who = { email: 'kai@example.org', image: null }
    const [withSecret, noSecret, http] = await Promise.all([
      badgedAvatarUrl({ SESSION_SECRET: 's' }, db, ORIGIN, who, 'failed'),
      badgedAvatarUrl({}, db, ORIGIN, { ...who, email: 'bo@example.org' }, 'failed'),
      badgedAvatarUrl({ SESSION_SECRET: 's' }, db, 'http://localhost:3253', who, 'failed'),
    ])
    expect([/\/a\/[0-9a-f]{64}\.png$/.test(withSecret ?? ''), noSecret, http, gets]).toEqual([true, null, null, []])
  })

  it('a face that won\'t load or decode leaves the plain face (null), never a throw', async () => {
    const { db } = await sqliteD1()
    const log = vi.spyOn(console, 'log').mockImplementation(() => {})
    serve(null)
    const missing = await badgedAvatarUrl({}, db, ORIGIN, { email: 'ann@example.org', image: FACE }, 'staged')
    serve(new TextEncoder().encode('{"ok":true}'))
    const notImage = await badgedAvatarUrl({}, db, ORIGIN, { email: 'ann@example.org', image: FACE }, 'staged')
    log.mockRestore()
    expect([missing, notImage]).toEqual([null, null])
  })
})
