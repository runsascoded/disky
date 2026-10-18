import { describe, expect, it } from 'vitest'
import { fromUrlPath, fromUrlSegs, isFold, mountedSegs, pathCopy, pathCrumbs, pathDisplay, pathLead, pathText, pathUri, storeSegsOfUrl, storeUrlSegs, toUrlPath, toUrlSegs, urlSegsOf } from './pathCrumbs'
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

describe('pathDisplay / pathText / pathCopy', () => {
  const home = ['Users', 'ryan']
  it('folds the store home to `~`, at and below it', () => {
    expect(pathDisplay('file:///', ['Users', 'ryan', 'c'], home)).toEqual({ lead: '~', leadSegs: 2, rest: ['c'] })
    expect(pathText('file:///', ['Users', 'ryan'], home)).toBe('~')
    expect(pathText('file:///', ['Users', 'ryan', 'c', 'disky'], home, true)).toBe('~/c/disky/')
  })
  it('a file store outside home reads as a plain absolute path', () => {
    expect(pathDisplay('file:///', ['Users'], home)).toEqual({ lead: '/', leadSegs: 0, rest: ['Users'] })
    expect(pathText('file:///', ['Applications', 'Slack.app'], home)).toBe('/Applications/Slack.app')
    expect(pathText('file:///', ['Users', 'ryanw'], home)).toBe('/Users/ryanw')
    expect(pathText('file:///', [], home)).toBe('/')
  })
  it('other schemes read as their URI, home or not', () => {
    expect(pathText('gs://', ['b', 'x'], undefined, true)).toBe('gs://b/x/')
    expect(pathText('r2://', [])).toBe('r2://')
  })
  it('copies a file store as an absolute path, other stores as the URI', () => {
    expect(pathCopy('file:///', ['Users', 'ryan', 'c'], true)).toBe('/Users/ryan/c/')
    expect(pathCopy('file:///', [])).toBe('/')
    expect(pathCopy('gs://', ['b', 'x'])).toBe('gs://b/x')
  })
})

describe('toUrlSegs / fromUrlSegs', () => {
  const home = ['Users', 'ryan']
  it('home folds to a leading `~` and expands back', () => {
    expect(toUrlSegs(['Users', 'ryan', 'c', 'disky'], home)).toEqual(['~', 'c', 'disky'])
    expect(toUrlSegs(['Users', 'ryan'], home)).toEqual(['~'])
    expect(fromUrlSegs(['~', 'c', 'disky'], home)).toEqual(['Users', 'ryan', 'c', 'disky'])
    expect(fromUrlSegs(['~'], home)).toEqual(['Users', 'ryan'])
  })
  it('paths outside home, and stores without one, pass through', () => {
    expect(toUrlSegs(['Applications', 'Slack.app'], home)).toEqual(['Applications', 'Slack.app'])
    expect(toUrlSegs(['Users'], home)).toEqual(['Users'])
    expect(toUrlSegs(['b', 'x'])).toEqual(['b', 'x'])
    expect(fromUrlSegs(['~', 'x'])).toEqual(['~', 'x'])
    expect(fromUrlSegs(['Users', 'ryan', 'c'], home)).toEqual(['Users', 'ryan', 'c'])
  })
})

describe('browser path encoding', () => {
  const home = ['Users', 'ryan']
  it('reloads a folder with spaces, URL delimiters, Unicode and a literal escape', () => {
    const segs = ['Users', 'ryan', 'Downloads', '2025 tax returns', '100% #1? café', '%20']
    expect(toUrlPath(segs, home)).toBe('~/Downloads/2025%20tax%20returns/100%25%20%231%3F%20caf%C3%A9/%2520')
    expect(fromUrlPath('~/Downloads/2025%20tax%20returns/100%25%20%231%3F%20caf%C3%A9/%2520', home)).toEqual(segs)
    expect(fromUrlPath('/bucket/100%/')).toEqual(['bucket', '100%'])
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

describe('storeUrlSegs / storeSegsOfUrl', () => {
  const laptop = { home: ['Users', 'ryan'] }
  const nas = { root: ['mnt', 'user'] }
  const both = { root: ['Volumes', 'x6'], home: ['Volumes', 'x6', 'ryan'] }
  it('folds a home to `~`, else leaves the mount out', () => {
    expect([
      storeUrlSegs(laptop, ['Users', 'ryan', 'c']),
      storeUrlSegs(laptop, ['Applications']),
      storeUrlSegs(nas, ['mnt', 'user', 'media']),
      storeUrlSegs(nas, ['mnt', 'user']),
      storeUrlSegs(both, ['Volumes', 'x6', 'ryan', 'c']),
      storeUrlSegs(both, ['Volumes', 'x6', 'other']),
    ]).toEqual([['~', 'c'], ['Applications'], ['media'], [], ['~', 'c'], ['other']])
  })
  it('reads each back, and a URL spelling the mount or home out as the same path', () => {
    expect([
      storeSegsOfUrl(laptop, ['~', 'c']),
      storeSegsOfUrl(laptop, ['Users', 'ryan', 'c']),
      storeSegsOfUrl(nas, ['media']),
      storeSegsOfUrl(nas, ['mnt', 'user', 'media']),
      storeSegsOfUrl(nas, []),
      storeSegsOfUrl(both, ['~', 'c']),
      storeSegsOfUrl(both, ['other']),
    ]).toEqual([
      ['Users', 'ryan', 'c'], ['Users', 'ryan', 'c'], ['mnt', 'user', 'media'], ['mnt', 'user', 'media'], ['mnt', 'user'],
      ['Volumes', 'x6', 'ryan', 'c'], ['Volumes', 'x6', 'other'],
    ])
  })
})
