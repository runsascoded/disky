import { describe, expect, it } from 'vitest'
import { hostPair, otherHostUrl, peerUrl, type Peer } from './hosts'

const pair = { prod: 'gcs.oa.dev', dev: 'dev.gcs.oa.dev' }

describe('hostPair', () => {
  it('needs both hosts', () => {
    expect(hostPair('gcs.oa.dev', 'dev.gcs.oa.dev')).toEqual(pair)
    expect(hostPair('gcs.oa.dev', '')).toBe(null)
    expect(hostPair(undefined, 'dev.gcs.oa.dev')).toBe(null)
  })
})

describe('otherHostUrl', () => {
  it('prod → dev, keeping path, query and hash', () => {
    expect(otherHostUrl('https://gcs.oa.dev/staged?q=hedy&s=-b#x', pair)).toBe('https://dev.gcs.oa.dev/staged?q=hedy&s=-b#x')
  })
  it('dev → prod', () => {
    expect(otherHostUrl('https://dev.gcs.oa.dev/?path=marin-us-east5%2Fcheckpoints', pair)).toBe('https://gcs.oa.dev/?path=marin-us-east5%2Fcheckpoints')
  })
  it('a local dev server goes to the dev host over https, port dropped', () => {
    expect(otherHostUrl('http://localhost:3253/users', pair)).toBe('https://dev.gcs.oa.dev/users')
  })
})

describe('peerUrl', () => {
  const cw = { label: 'CoreWeave usage', prod: 'cw.oa.dev', dev: 'dev.cw.oa.dev' }
  const go = (href: string, fromDev = false, peer: Peer = cw) => peerUrl(href, peer, fromDev)
  it('a page keeps its route, query and hash', () => {
    expect([
      go('https://gcs.oa.dev/scans?kind=scan&status=failed#runs'),
      go('https://gcs.oa.dev/health'),
      go('https://gcs.oa.dev/admin/db/scan_runs?n=50'),
      go('https://gcs.oa.dev/user/percy-liang'),
    ]).toEqual([
      'https://cw.oa.dev/scans?kind=scan&status=failed#runs',
      'https://cw.oa.dev/health',
      'https://cw.oa.dev/admin/db/scan_runs?n=50',
      'https://cw.oa.dev/user/percy-liang',
    ])
  })
  it("a page stops at this deployment's ids", () => {
    expect([
      go('https://gcs.oa.dev/scans/26101707'),
      go('https://gcs.oa.dev/runs/abc/def'),
      go('https://gcs.oa.dev/admin/sessions/s1'),
    ]).toEqual([
      'https://cw.oa.dev/scans',
      'https://cw.oa.dev/staged',
      'https://cw.oa.dev/admin/sessions',
    ])
  })
  it("a drill path becomes the peer's root, keeping the view's params less this deployment's", () => {
    expect(go('https://gcs.oa.dev/marin-us-east5/checkpoints?d=26101707&c=read&f=grug,llama&o=calvin-xu&iec=1&open=x%2Fy#h'))
      .toBe('https://cw.oa.dev/?c=read&f=grug,llama&iec=1')
  })
  it('dev → the peer\'s dev host; a peer without one, or from prod or localhost → its prod', () => {
    expect([
      go('https://dev.gcs.oa.dev/scans', true),
      go('https://dev.gcs.oa.dev/scans', true, { label: 'HF usage', prod: 'hf.oa.dev' }),
      go('http://localhost:3253/scans'),
    ]).toEqual([
      'https://dev.cw.oa.dev/scans',
      'https://hf.oa.dev/scans',
      'https://cw.oa.dev/scans',
    ])
  })
})
