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
  const cw = { label: 'CoreWeave usage', prod: 'cw.oa.dev', dev: 'dev.cw.oa.dev', staging: true, meta: true }
  const gcs = { label: 'GCS usage', prod: 'gcs.oa.dev', owners: true, staging: true, meta: true }
  const hf = { label: 'HF usage', prod: 'hf.oa.dev' }
  const go = (href: string, fromDev = false, peer: Peer = cw) => peerUrl(href, peer, fromDev)
  it('a page keeps its route, query and hash', () => {
    expect([
      go('https://gcs.oa.dev/scans?kind=scan&status=failed#runs'),
      go('https://gcs.oa.dev/health'),
      go('https://gcs.oa.dev/admin/db/scan_runs?n=50'),
      go('https://cw.oa.dev/user/percy-liang', false, gcs),
      go('https://gcs.oa.dev/staged?q=hedy'),
    ]).toEqual([
      'https://cw.oa.dev/scans?kind=scan&status=failed#runs',
      'https://cw.oa.dev/health',
      'https://cw.oa.dev/admin/db/scan_runs?n=50',
      'https://gcs.oa.dev/user/percy-liang',
      'https://cw.oa.dev/staged?q=hedy',
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
  it("the meta tree goes to the peer's, its drill path dropped; a peer without one → its root", () => {
    expect([
      go('https://r2.oa.dev/meta'),
      go('https://r2.oa.dev/meta/oa-r2-usage-index/static-names?d=26101804&c=written#h'),
      go('https://r2.oa.dev/meta/files/oa-r2-usage-index/x.parquet'),
      go('https://r2.oa.dev/meta?c=written', false, hf),
    ]).toEqual([
      'https://cw.oa.dev/meta',
      'https://cw.oa.dev/meta?c=written',
      'https://cw.oa.dev/meta',
      'https://hf.oa.dev/?c=written',
    ])
  })
  it("a page the peer doesn't serve (owners, staging) → its root", () => {
    expect([
      go('https://gcs.oa.dev/user/percy-liang?p=marin-us-east5%2Fcheckpoints'),
      go('https://gcs.oa.dev/users#top'),
      go('https://gcs.oa.dev/assignments?by=dlwh'),
      go('https://gcs.oa.dev/staged?q=hedy', false, hf),
      go('https://gcs.oa.dev/runs/abc', false, hf),
    ]).toEqual([
      'https://cw.oa.dev/',
      'https://cw.oa.dev/',
      'https://cw.oa.dev/',
      'https://hf.oa.dev/',
      'https://hf.oa.dev/',
    ])
  })
  it("drops this deployment's ids: a user page's drill, an assigner, a run, a sign-in target, legacy scan ids", () => {
    expect([
      go('https://cw.oa.dev/user/percy-liang?p=marin-us-east5%2Fx&iec=1', false, gcs),
      go('https://cw.oa.dev/assignments?by=dlwh&n=50', false, gcs),
      go('https://gcs.oa.dev/staged?run=r1&rs=done'),
      go('https://gcs.oa.dev/signin?next=%2Fmarin-us-east5%2Fcheckpoints'),
      go('https://gcs.oa.dev/names?date=26101707&from=26101007&f=llama'),
    ]).toEqual([
      'https://gcs.oa.dev/user/percy-liang?iec=1',
      'https://gcs.oa.dev/assignments?n=50',
      'https://cw.oa.dev/staged?rs=done',
      'https://cw.oa.dev/signin',
      'https://cw.oa.dev/names?f=llama',
    ])
  })
  it("a drill path becomes the peer's root, keeping the view's params less this deployment's", () => {
    expect(go('https://gcs.oa.dev/marin-us-east5/checkpoints?d=26101707&c=read&f=grug,llama&o=calvin-xu&iec=1&open=x%2Fy#h'))
      .toBe('https://cw.oa.dev/?c=read&f=grug,llama&iec=1')
  })
  it("the meta store is every deployment's: its root and any drill into it → the peer's meta root", () => {
    expect([
      go('https://gcs.oa.dev/meta?d=26101707&c=tree'),
      go('https://gcs.oa.dev/meta/cw-l2/2026-10-17?c=tree#h'),
    ]).toEqual([
      'https://cw.oa.dev/meta?c=tree',
      'https://cw.oa.dev/meta?c=tree',
    ])
  })
  it('dev → the peer\'s dev host; a peer without one, or from prod or localhost → its prod', () => {
    expect([
      go('https://dev.gcs.oa.dev/scans', true),
      go('https://dev.gcs.oa.dev/scans', true, hf),
      go('http://localhost:3253/scans'),
    ]).toEqual([
      'https://dev.cw.oa.dev/scans',
      'https://hf.oa.dev/scans',
      'https://cw.oa.dev/scans',
    ])
  })
})
