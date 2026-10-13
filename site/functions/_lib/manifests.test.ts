import { describe, expect, it } from 'vitest'
import { MANIFEST_NAME, newestManifest } from './manifests'

// The one newest-manifest rule both run-store readers use (`latestManifest`, `ivManifest`): a scan's `<id>.json`, a merge's
// revision `<id>.mNNN.json` after it, every later scan's after that; anything else under `manifests/` is not a manifest.
describe('newestManifest', () => {
  it('takes revisions in order, then later scans', () => {
    const names = ['2026-10-09.json', '2026-10-09.m001.json', '2026-10-09.m002.json', '2026-10-09T1236.json', '2026-10-10.json', '2026-10-10.m001.json']
    for (let i = 1; i <= names.length; i++) expect(newestManifest([...names.slice(0, i)].reverse())).toEqual(names[i - 1])
    expect(newestManifest([])).toBeNull()
  })

  it('ignores names that are not manifests, even ones that would sort last', () => {
    // A bare `[^/]+\.json` filter would pick each of these over `2026-10-10.m001.json`.
    const junk = ['2026-10-10.m1.json', '2026-10-10.m0001.json', 'scans.json', '2026-10-10.m001.json.tmp', 'x.json', '2026-10-10/meta.json', '2026-10-10T12.json']
    expect(newestManifest(['2026-10-09.json', '2026-10-10.m001.json', ...junk])).toEqual('2026-10-10.m001.json')
    expect(junk.map(n => MANIFEST_NAME.test(n))).toEqual(junk.map(() => false))
    expect(newestManifest(junk)).toBeNull()
  })
})
