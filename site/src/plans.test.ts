import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider, type QueryObserverOptions } from '@tanstack/react-query'
import { expect, it, vi } from 'vitest'
import { usePrefixHistory } from './plans'

vi.mock('./stores', () => ({ DEFAULT_STORE: { key: 'cw', executor: 'plan-sweep' } }))

it('loads CW per-prefix deletion outcomes even when its executor does not expose run files', () => {
  const client = new QueryClient()
  function History() { usePrefixHistory(2, false); return null }
  renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(History)))
  const options = client.getQueryCache().find({ queryKey: ['prefix-history', 2] })?.options as QueryObserverOptions | undefined
  expect(options?.enabled).toBe(true)
  client.clear()
})
