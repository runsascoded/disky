import { describe, expect, it } from 'vitest'
import { pageTitle, renderPage, resolvePath, rewriteHref } from './docs'

const REPO = 'https://github.com/o/r'
const pages = [
  { slug: '', path: 'fleet/docs/index.md', md: '# Front door\n\nSee [the engine](../../README.md#install) and [a spec](../../specs/x.md).\n\n![card](../../site/public/og.jpg)\n' },
  { slug: 'disky', path: 'README.md', md: 'no heading\n' },
]

describe('docs links', () => {
  it('resolves relative paths inside the repo, and refuses one that climbs out', () => {
    expect([resolvePath('fleet/docs', '../../README.md'), resolvePath('', './a/./b.md'), resolvePath('a', '../../x')]).toEqual(['README.md', 'a/b.md', null])
  })
  it('points a link at the page that serves its file, else at the file on the repo host; absolute and anchor links pass', () => {
    const from = 'fleet/docs/index.md'
    expect([
      rewriteHref('../../README.md#install', from, pages, REPO, 'main'),
      rewriteHref('../../specs/x.md', from, pages, REPO, 'main'),
      rewriteHref('../../site/public/og.jpg', from, pages, REPO, 'main', true),
      rewriteHref('https://gcs.oa.dev', from, pages, REPO, 'main'),
      rewriteHref('#top', from, pages, REPO, 'main'),
      rewriteHref('/fleet', from, pages, REPO, 'main'),
      rewriteHref('fleet/docs/index.md', 'README.md', pages, REPO, 'main'),
    ]).toEqual([
      '/docs/disky#install',
      'https://github.com/o/r/blob/main/specs/x.md',
      'https://github.com/o/r/raw/main/site/public/og.jpg',
      'https://gcs.oa.dev',
      '#top',
      '/fleet',
      '/',
    ])
  })
  it('renders a page with its links rewritten', () => {
    expect(renderPage(pages[0], pages, REPO, 'main').split('\n')).toEqual([
      '<h1>Front door</h1>',
      '<p>See <a href="/docs/disky#install">the engine</a> and <a href="https://github.com/o/r/blob/main/specs/x.md">a spec</a>.</p>',
      '<p><img src="https://github.com/o/r/raw/main/site/public/og.jpg" alt="card"></p>',
      '',
    ])
  })
  it('titles a page by its first heading, else its file name', () => {
    expect(pages.map(pageTitle)).toEqual(['Front door', 'README'])
  })
})
