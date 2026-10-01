import { describe, expect, it } from 'vitest'
import { fromUrlSegs, isFold, pathCopy, pathCrumbs, pathDisplay, pathText, pathUri, toUrlSegs } from './pathCrumbs'
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
