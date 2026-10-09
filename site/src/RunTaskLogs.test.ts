import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { expect, it } from 'vitest'
import { RunTaskLogs } from './RunTaskLogs'

it('renders text as escaped text, UTC times, and a link rather than running anything', () => {
  const client = new QueryClient()
  client.setQueryData(['batch-task-logs', 'job', 'region'], { pageParams: [null], pages: [{ entries: [{ id: 'i', timestamp: '2026-10-05T16:26:00.123Z', severity: 'INFO', message: '<script>delete()</script>', truncated: false }], nextPageToken: null, until: 'now' }] })
  const html = renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(RunTaskLogs, { job: 'job', region: 'region', live: false, explorer: 'https://console.example/logs' })))
  const pre = html.match(/<pre>(.*?)<\/pre>/)?.[1]
  expect(pre).toBe('&lt;script&gt;delete()&lt;/script&gt;')
  expect(html.match(/<time[^>]+>(.*?)<\/time>/)?.[1]).toBe('2026-10-05 16:26:00Z')
  expect(html.match(/<a href="([^"]+)"[^>]*>(.*?)<\/a>/)?.slice(1)).toEqual(['https://console.example/logs', 'Logs Explorer ↗'])
  client.clear()
})
