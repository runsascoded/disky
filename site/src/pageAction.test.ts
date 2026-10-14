import { describe, expect, it } from 'vitest'
import { searchActions, type ActionRegistry } from 'use-kbd'
import { pageAction } from './pageAction'

const pages = [
  ['/scans', 'Scans — job runs'],
  ['/staged', 'Staged deletions'],
  ['/health', 'Health'],
  ['/admin', 'Admin'],
  ['/admin/db', 'Database'],
  ['/admin/sessions', 'Session logs'],
  ['/privacy', 'Privacy policy'],
] as const
const actions: ActionRegistry = Object.fromEntries([
  ...Array.from({ length: 30 }, (_, i) => [`scan:${i}`, { label: `Scan 10/${30 - i}`, group: 'Scans', handler: () => {} }]),
  ...pages.map(([path, title]) => [`page:${path}`, { ...pageAction(path, title), handler: () => {} }]),
])

describe('page navigation in the omnibar', () => {
  it('states the destination and exact search keywords', () => {
    expect(pageAction('/scans', 'Scans — job runs')).toEqual({
      label: 'Scans — job runs · /scans', group: 'Pages', keywords: ['/scans', 'scans'],
    })
  })
  it.each(['scans', '/scans'])('ranks the scans page before thirty snapshot actions for %s', query => {
    const results = searchActions(query, actions).slice(0, 15)
    expect(results.filter(r => r.id.startsWith('page:')).map(r => r.id)).toEqual(['page:/scans'])
    expect(results[0].id).toBe('page:/scans')
  })
  it.each(pages.flatMap(([path]) => [path, path.slice(1)].map(query => [query, `page:${path}`])))('finds %s by its URL', (query, id) => {
    expect(searchActions(query, actions)[0].id).toBe(id)
  })
})
