import { describe, expect, it } from 'vitest'
import { imageParams, splitImageParams } from './cred'

describe('image params ⇄ view + version + credential', () => {
  it('round-trips, and a bare view carries no credential', () => {
    const view = { path: 'marin-a', d: '261002' }
    expect([
      imageParams(view, null, { t: 'TokAAAAAAA' }),
      imageParams({}, 'abcdef01', { g: 'grant123' }),
      imageParams(view, null, null),
      splitImageParams({ path: 'marin-a', d: '261002', t: 'TokAAAAAAA' }),
      splitImageParams({ v: 'abcdef01', g: 'grant123' }),
      splitImageParams({ path: 'marin-a' }),
      splitImageParams({ path: 'marin-a', s: '2026-10-12T1201', r: '2', policy: 'generic', t: 'TokAAAAAAA' }),
    ]).toEqual([
      { path: 'marin-a', d: '261002', t: 'TokAAAAAAA' },
      { v: 'abcdef01', g: 'grant123' },
      { path: 'marin-a', d: '261002' },
      { view: { path: 'marin-a', d: '261002' }, cred: { t: 'TokAAAAAAA' } },
      { view: {}, v: 'abcdef01', cred: { g: 'grant123' } },
      { view: { path: 'marin-a' }, cred: null },
      { view: { path: 'marin-a' }, scan: '2026-10-12T1201', cred: { t: 'TokAAAAAAA' } },
    ])
  })
})
