// Activity posts' sender faces (`publicAvatar.ts`, `activity.senderOf`) and the public route that serves them
// (`functions/a/[[path]].ts`): content-addressed, HMAC-seeded identicons, nothing reachable but a stored image's hash.
import { describe, expect, it } from 'vitest'
import { validateUploadedImage } from '@open-athena/auth'
import { sqliteD1 } from './testD1'
import { senderOf } from './activity'
import { mergeKinds, reported } from './activityShape'
import { AVATAR_PATH, dataUriImage, identiconPng, identiconSeed, publishAvatar, readAvatar, sha256Hex } from './publicAvatar'
import { onRequest } from '../a/[[path]]'

const ORIGIN = 'https://cw.example.test'
const NOW = Date.UTC(2026, 10, 3, 18, 0, 0)
const S = Math.floor(NOW / 1000)
const SECRET = 'test-session-secret'
const enc = new TextEncoder()
const dataUri = (bytes: Uint8Array): string => `data:image/png;base64,${btoa(String.fromCharCode(...bytes))}`

describe('identicons', () => {
  it('are valid 120 px PNGs, the same for the same seed and different across seeds', async () => {
    const a = identiconPng(await identiconSeed(SECRET, 'ann@example.test'))
    const v = validateUploadedImage(a)
    expect([v.type, v.bytes.length === a.length]).toEqual(['image/png', true])
    expect(await sha256Hex(identiconPng(await identiconSeed(SECRET, 'ann@example.test')))).toBe(await sha256Hex(a))
    const others = await Promise.all(['bob@example.test', 'grant:cw-scan-job', 'grant:2EjUTg692KMP'].map(async x => sha256Hex(identiconPng(await identiconSeed(SECRET, x)))))
    expect(new Set([await sha256Hex(a), ...others]).size).toBe(4)
  })

  it('are seeded by an HMAC under the secret: not a bare digest of the actor, and a different secret differs', async () => {
    const seed = await identiconSeed(SECRET, 'ann@example.test')
    const bare = new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode('ann@example.test')))
    expect([
      seed.length,
      seed.every((b, i) => b === bare[i]),
      (await identiconSeed('other-secret', 'ann@example.test')).every((b, i) => b === seed[i]),
    ]).toEqual([32, false, false])
  })
})

describe('stored faces', () => {
  it('a data: URI face is validated raster bytes; anything else is none', () => {
    const png = identiconPng(new Uint8Array([1, 2, 3, 4]))
    expect([
      dataUriImage(dataUri(png))?.type,
      dataUriImage(null), dataUriImage('asset://x'), dataUriImage('https://example.test/a.png'),
      dataUriImage('data:image/png;base64,PGh0bWw+'), dataUriImage('data:image/svg+xml;base64,PHN2Zz4='),
    ]).toEqual(['image/png', null, null, null, null, null])
  })

  it('publishing stores the bytes once under their SHA-256 and names only that hash', async () => {
    const { db, raw } = await sqliteD1()
    const png = identiconPng(new Uint8Array([9, 9, 9, 9]))
    const sha = await sha256Hex(png)
    const url = await publishAvatar(db, ORIGIN, { type: 'image/png', bytes: png }, S)
    expect(await publishAvatar(db, ORIGIN, { type: 'image/png', bytes: png }, S + 60)).toBe(url)
    expect([url, raw.prepare('SELECT sha256, type, created_at FROM public_avatars').all()]).toEqual([
      `${ORIGIN}/a/${sha}.png`, [{ sha256: sha, type: 'image/png', created_at: S }],
    ])
    expect((await readAvatar(db, `/a/${sha}.png`))?.bytes).toEqual(png)
  })
})

describe('the public route', () => {
  async function stored() {
    const { db, raw } = await sqliteD1()
    const png = identiconPng(new Uint8Array([5, 6, 7, 8]))
    const sha = await sha256Hex(png)
    await publishAvatar(db, ORIGIN, { type: 'image/png', bytes: png }, S)
    raw.prepare("INSERT INTO grants (id, token_hash, name, scopes, created_at, created_by) VALUES ('2EjUTg692KMP', 'h1', 'cw-scan-job', 'cw:read', ?, 'ops')").run(S)
    const get = async (path: string, method = 'GET') => {
      const r = await onRequest({ request: new Request(`${ORIGIN}${path}`, { method }), env: { DB: db } })
      return [r.status, r.headers.get('content-type'), r.headers.get('cache-control')]
    }
    return { db, png, sha, get }
  }

  it('serves a stored hash, with or without an image extension, immutably', async () => {
    const { db, png, sha, get } = await stored()
    const r = await onRequest({ request: new Request(`${ORIGIN}/a/${sha}.png`), env: { DB: db } })
    expect([r.status, [...r.headers].sort(), new Uint8Array(await r.arrayBuffer())]).toEqual([200, [
      ['cache-control', 'public, max-age=31536000, immutable'],
      ['content-security-policy', "default-src 'none'"],
      ['content-type', 'image/png'],
      ['x-content-type-options', 'nosniff'],
    ], png])
    expect([await get(`/a/${sha}`), await get(`/a/${sha}.jpg`), await get(`/a/${sha}.png`, 'HEAD')]).toEqual([
      [200, 'image/png', 'public, max-age=31536000, immutable'],
      [200, 'image/png', 'public, max-age=31536000, immutable'],
      [200, 'image/png', 'public, max-age=31536000, immutable'],
    ])
  })

  it('404s everything else: an unknown hash, an email, a grant id or name, a non-sha256, a prefix, the bare route', async () => {
    const { sha, get } = await stored()
    const emailSha = await sha256Hex(enc.encode('ann@example.test'))
    const gone = [404, 'text/plain; charset=utf-8', 'no-store']
    const paths = [
      `/a/${'0'.repeat(64)}.png`, `/a/${emailSha}.png`, '/a/ann@example.test', '/a/ann%40example.test', '/a/2EjUTg692KMP',
      '/a/cw-scan-job', `/a/${sha.slice(0, 40)}`, `/a/${sha.toUpperCase()}`, `/a/${sha}.svg`, `/a/${sha}/x`, '/a/', '/a',
    ]
    expect(await Promise.all(paths.map(p => get(p)))).toEqual(paths.map(() => gone))
    expect([AVATAR_PATH.test(`/a/${sha}.png`), AVATAR_PATH.test(`/a/${sha}.html`)]).toEqual([true, false])
  })

  it('404s on a database without the table: a deployment still waiting for the `public-avatars` recipe', async () => {
    const { db, raw } = await sqliteD1()
    raw.exec('DROP TABLE public_avatars')
    const res = await onRequest({ request: new Request(`${ORIGIN}/a/${'0'.repeat(64)}.png`), env: { DB: db } })
    expect([res.status, await res.text()]).toEqual([404, 'not found\n'])
  })

  it('takes reads only', async () => {
    const { sha } = await stored()
    const r = await onRequest({ request: new Request(`${ORIGIN}/a/${sha}.png`, { method: 'POST' }), env: {} })
    expect([r.status, r.headers.get('allow')]).toEqual([405, 'GET, HEAD'])
  })
})

describe('senders', () => {
  const tiny = identiconPng(new Uint8Array([1, 1, 2, 2]))
  async function setup() {
    const { db, raw } = await sqliteD1()
    raw.prepare('INSERT INTO profiles (email, name, avatar, avatar_src, updated_at) VALUES (?, ?, ?, ?, ?)').run('ann@example.test', 'Ann Lee', dataUri(tiny), 'upload', S)
    raw.prepare('INSERT INTO profiles (email, name, avatar, avatar_src, updated_at) VALUES (?, ?, NULL, NULL, ?)').run('bob@example.test', null, S)
    const grant = (id: string, name: string | null, subject: object | null) =>
      raw.prepare('INSERT INTO grants (id, token_hash, name, subject_json, scopes, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)')
        .run(id, `h-${id}`, name, subject ? JSON.stringify(subject) : null, 'cw:read', S, 'ops')
    grant('2EjUTg692KMP', null, null)
    grant('8ryPLkfsLwhC', 'loombot-link', { name: 'LoomBot', avatar: dataUri(tiny) })
    grant('Gx0000000001', 'gcs-snapshot-job', null)
    return { db, env: { DB: db, SESSION_SECRET: SECRET } }
  }
  const gen = async (actor: string) => `${ORIGIN}/a/${await sha256Hex(identiconPng(await identiconSeed(SECRET, actor)))}.png`

  it('a profile\'s name and face; a grant\'s holder name and face, else its label, else its id prefix; else a generated face', async () => {
    const { env } = await setup()
    const face = `${ORIGIN}/a/${await sha256Hex(tiny)}.png`
    const as = (actor: string, via: string) => senderOf(env, { actor, via, origin: ORIGIN }, NOW)
    expect(await Promise.all([
      as('ann@example.test', 'session'),
      as('bob@example.test', 'session'),
      as('grant:8ryPLkfsLwhC', 'grant'),
      as('grant:gcs-snapshot-job', 'grant'),
      as('grant:2EjUTg692KMP', 'grant'),
      as('grant', 'grant'),
    ])).toEqual([
      { name: 'Ann Lee', icon: face },
      { name: 'bob', icon: await gen('bob@example.test') },
      { name: 'LoomBot', icon: face },
      { name: 'gcs-snapshot-job', icon: await gen('grant:gcs-snapshot-job') },
      { name: 'grant 2EjU', icon: await gen('grant:2EjUTg692KMP') },
      { name: 'grant', icon: await gen('grant') },
    ])
  })

  it('no generated face without the secret; a generated face\'s URL is no unsalted digest of the email', async () => {
    const { db, env } = await setup()
    expect(await senderOf({ DB: db }, { actor: 'bob@example.test', via: 'session', origin: ORIGIN }, NOW)).toEqual({ name: 'bob', icon: null })
    const { icon } = await senderOf(env, { actor: 'bob@example.test', via: 'session', origin: ORIGIN }, NOW)
    const digests = await Promise.all(['SHA-256', 'SHA-1', 'SHA-384'].map(async h => [...new Uint8Array(await crypto.subtle.digest(h, enc.encode('bob@example.test')))].map(b => b.toString(16).padStart(2, '0')).join('')))
    expect(digests.map(d => icon!.includes(d))).toEqual([false, false, false])
    expect(icon!.includes('bob')).toBe(false)
  })
})

describe('digest syncs', () => {
  it('are logged but never notified by default', () => {
    const conf = mergeKinds([])
    expect([conf.digest, reported({ digest: 3 }, conf), reported({ digest: 3, write: 1 }, conf)]).toEqual([{ log: true, notify: 'off' }, false, true])
  })
})
