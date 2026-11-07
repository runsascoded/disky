import { describe, expect, it } from 'vitest'
import { fromUrlPath, isFold, mountedSegs, pathCopy, pathCrumbs, pathLead, pathText, pathUri, toUrlPath, urlSegsOf } from './pathCrumbs'
import type { Crumb } from './pathCrumbs'

describe('pathCrumbs', () => {
  it('every ancestor drills; the deepest segment is inert', () => {
    expect(pathCrumbs(['checkpoints', 'run-1', 'step-100'])).toEqual<Crumb[]>([
      { name: 'checkpoints', segs: ['checkpoints'], drillable: true, last: false },
      { name: 'run-1', segs: ['checkpoints', 'run-1'], drillable: true, last: false },
      { name: 'step-100', segs: ['checkpoints', 'run-1', 'step-100'], drillable: false, last: true },
    ])
  })
  it('a folded `(other)` never drills, wherever it sits', () => {
    expect(pathCrumbs(['(other)', 'x'])).toEqual<Crumb[]>([
      { name: '(other)', segs: ['(other)'], drillable: false, last: false },
      { name: 'x', segs: ['(other)', 'x'], drillable: false, last: true },
    ])
    expect(isFold('(files)')).toBe(true)
    expect(isFold('files')).toBe(false)
  })
  it('the root has no crumbs', () => {
    expect(pathCrumbs([])).toEqual([])
  })
})

describe('pathUri', () => {
  it('joins the segments under the store’s scheme', () => {
    expect(pathUri('gs://', ['marin-us-central2', 'checkpoints'])).toBe('gs://marin-us-central2/checkpoints')
    expect(pathUri('s3://', ['hero-checkpoints'])).toBe('s3://hero-checkpoints')
    expect(pathUri('r2://', [])).toBe('r2://')
  })
  it('a directory prefix ends in `/`, except the root', () => {
    expect(pathUri('gs://', ['marin-us-central2', 'checkpoints'], true)).toBe('gs://marin-us-central2/checkpoints/')
    expect(pathUri('gs://', [], true)).toBe('gs://')
  })
})

describe('pathLead / pathText / pathCopy', () => {
  it('a file store reads as a plain absolute path', () => {
    expect([pathLead('file:///'), pathText('file:///', ['Applications', 'Slack.app']), pathText('file:///', ['Users', 'ryan', 'c'], true), pathText('file:///', [])])
      .toEqual(['/', '/Applications/Slack.app', '/Users/ryan/c/', '/'])
  })
  it('other schemes read as their URI', () => {
    expect([pathLead('gs://'), pathText('gs://', ['b', 'x'], true), pathText('r2://', [])]).toEqual(['gs://', 'gs://b/x/', 'r2://'])
  })
  it('copies a file store as an absolute path, other stores as the URI', () => {
    expect(pathCopy('file:///', ['Users', 'ryan', 'c'], true)).toBe('/Users/ryan/c/')
    expect(pathCopy('file:///', [])).toBe('/')
    expect(pathCopy('gs://', ['b', 'x'])).toBe('gs://b/x')
  })
})

describe('browser path encoding', () => {
  it('round-trips a folder with spaces, URL delimiters, Unicode and a literal escape', () => {
    const segs = ['bk', 'Downloads', '2025 tax returns', '100% #1? café', '%20']
    expect(toUrlPath(segs)).toBe('bk/Downloads/2025%20tax%20returns/100%25%20%231%3F%20caf%C3%A9/%2520')
    expect(fromUrlPath('/bk/Downloads/2025%20tax%20returns/100%25%20%231%3F%20caf%C3%A9/%2520')).toEqual(segs)
  })
  it('a hand-written literal `%` stays a name; empty segments drop', () => {
    expect(fromUrlPath('/bucket//100%/')).toEqual(['bucket', '100%'])
  })
})

describe('a store’s mount (`Store.root`)', () => {
  const root = ['open-athena']
  it('leads every path read off a URL; a URL from before the mount (the root spelled out) reads the same', () => {
    expect([[], ['models'], ['open-athena'], ['open-athena', 'models']].map(u => mountedSegs(root, u))).toEqual([
      ['open-athena'], ['open-athena', 'models'], ['open-athena'], ['open-athena', 'models'],
    ])
    expect(mountedSegs(undefined, ['bk', 'x'])).toEqual(['bk', 'x'])
  })
  it('is left out of a path’s URL segments', () => {
    expect([['open-athena'], ['open-athena', 'models'], ['other']].map(s => urlSegsOf(root, s))).toEqual([[], ['models'], ['other']])
    expect(urlSegsOf([], ['bk'])).toEqual(['bk'])
  })
})
