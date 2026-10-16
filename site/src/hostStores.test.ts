import { describe, expect, it } from 'vitest'
import { hostStore, resolveStores, withHosts } from './stores'

// The capture hosts' stores (specs/hosts-registry.md): the edge stamps each member's page with D1 `hosts`;
// the client mounts each one as a secondary store, behind any store the build already has.

const built = resolveStores('laptop', 'nas')

describe('hostStore', () => {
  it('is a file store under `/<store>`, its `~` and root from the row', () => {
    expect(hostStore({ id: 'm1', store: 'm1', label: 'M1', home: 'Users/ryan', mark: 'laptop' })).toEqual({
      key: 'm1',
      label: 'M1',
      title: 'disk-tree — M1',
      desc: 'Disk usage of M1, captured by the disky app.',
      path: '/m1',
      mark: 'laptop',
      scheme: 'file:///',
      home: ['Users', 'ryan'],
      base: '/data/m1',
      ogImage: '/og.jpg',
      prices: false,
      staging: false,
      owners: false,
      executor: 'laptop',
      buckets: [],
      rootLabel: 'all roots',
      objectsNote: 'Local files; created is each file’s mtime at capture time.',
      wall: { restrict: 'M1’s disk usage is private to its owner.', signIn: 'Sign in' },
    })
    expect(hostStore({ id: 'nas2', store: 'nas2', label: 'NAS 2', root: '/mnt/user/' }).root).toEqual(['mnt', 'user'])
  })
})

describe('withHosts', () => {
  it('appends each new host; the build’s store keeps its key and path; a malformed stamp is ignored', () => {
    const keys = (hosts: unknown) => withHosts(built, hosts).map(s => [s.key, s.path])
    expect(keys([
      { id: 'nas', store: 'nas', label: 'NAS', root: 'mnt/user' },
      { id: 'm1', store: 'm1', label: 'M1' },
      { id: 'bad', store: 'Bad Key', label: 'x' },
      { id: 'nolabel', store: 'nolabel' },
      null,
    ])).toEqual([['laptop', '/'], ['nas', '/nas'], ['m1', '/m1']])
    expect(withHosts(built, undefined)).toBe(built)
    expect(withHosts(built, { m1: {} })).toBe(built)
    expect(withHosts(built, [{ id: 'nas', store: 'nas', label: 'Other' }]).find(s => s.key === 'nas')?.label).toBe('NAS')
  })
})
