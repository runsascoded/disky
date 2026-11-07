import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { expect, it } from 'vitest'
import { PreviewLinks, previewHref } from './PreviewLinks'

it('describes plain previews without promising a particular deployment policy', () => {
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: new QueryClient() },
    createElement(MemoryRouter, null, createElement(PreviewLinks))))
  expect(html).toBe('<section class="preview-links"><h2>Preview links</h2><p>'
    + 'Links minted with “Copy link with preview”: their unfurl card shows the full view (labels, sizes, owners) for that exact page and params. '
    + 'Plain links follow this site&#x27;s anonymous preview policy and carry no detailed-preview token. Copy a live one to share it again; revoke to send a view back to the anonymous card.'
    + '</p><div class="table-scroll"><table class="grants"><thead><tr><th>page</th><th>minted by</th><th>when</th><th>until</th><th></th></tr></thead><tbody></tbody></table></div></section>')
})

it('rebuilds a mint\'s link from its row: the page (params and hash kept) plus `og=`', () => {
  expect([
    previewHref('/', 'AbC123', 'https://gcs.oa.dev'),
    previewHref('/?section=diff&d=261016-260916', 'AbC123', 'https://gcs.oa.dev'),
    previewHref('/marin-us-central2/grug?c=w#over-time', 'AbC123', 'https://gcs.oa.dev'),
  ]).toEqual([
    'https://gcs.oa.dev/?og=AbC123',
    'https://gcs.oa.dev/?section=diff&d=261016-260916&og=AbC123',
    'https://gcs.oa.dev/marin-us-central2/grug?c=w&og=AbC123#over-time',
  ])
})
