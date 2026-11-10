import { expect, test, type Page } from '@playwright/test'

test.skip(process.env.CLEANUP_AGENT_PREVIEW !== '1', 'set CLEANUP_AGENT_PREVIEW=1 against a laptop deployment')
test.use({ actionTimeout: 5000, colorScheme: 'dark' })

const PREFIX = 'file:///Users/demo/Downloads/example/'
const NOW = 1794315600
const RUN = {
  run_id: 'laptop-real-fixture', plan_id: 1, mode: 'real', actor: 'dev@example.test',
  started_ts: NOW - 60, finished_ts: null, executor_started_ts: null, selected_items_json: JSON.stringify([PREFIX]), execution_errors_json: null,
  deleted_bytes: 0, deleted_objects: 0, skipped_gone: 0, undo_deadline: null, undo_state: 'none', log_dir: 'laptop',
}

async function fixture(page: Page, initialReady = false) {
  let ready = initialReady
  let statusError = false
  let runs: object[] = []
  const dispatches: object[] = []
  // Every app API request is intercepted. No request can reach a real drainer.
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/executor-status') return route.fulfill({ status: statusError ? 503 : 200, json: statusError ? { error: 'Status service unavailable' } : { executor: 'laptop', ready, last_seen: ready ? NOW - 5 : NOW - 45391, checked_at: NOW, stale_after: 180 } })
    if (path === '/api/auth/whoami' || path === '/api/whoami') return route.fulfill({ json: { kind: 'sso', email: 'dev@example.test', admin: true, scopes: ['laptop', 'admin'], subject: null } })
    if (path === '/api/plans/staged') return route.fulfill({ json: {
      plan: { id: 1, name: 'Staged', state: 'open', created_by: 'dev@example.test', created_ts: NOW - 100 },
      items: [{ prefix: PREFIX, kind: 'prefix', note: null, added_by: 'dev@example.test', added_ts: NOW - 100, batch_id: null, as_of: '2026-11-10T1200' }], batches: [], runs,
    } })
    if (path === '/api/prefixes') return route.fulfill({ json: { stats: { [PREFIX]: { b: 100000000, o: 20 } } } })
    if (path === '/api/plans/prefix-history') return route.fulfill({ json: { bands: [] } })
    if (path === '/api/plan-sweep/dispatch') {
      dispatches.push(route.request().postDataJSON())
      runs = [{ ...RUN, mode: 'dry', run_id: 'laptop-dry-fixture' }]
      return route.fulfill({ json: { job_id: 'laptop-dry-fixture' } })
    }
    return route.fulfill({ json: {} })
  })
  await page.route('**/data/**', route => route.fulfill({ json: route.request().url().endsWith('/scans.json') ? ['2026-11-10T1200'] : { users: [] } }))
  return {
    dispatches,
    ready: () => { ready = true },
    error: () => { statusError = true },
    runs: (value: object[]) => { runs = value },
  }
}

test('an offline cleanup agent is visible, recoverable by checking again, and never auto-dispatches', async ({ page }) => {
  const f = await fixture(page)
  await page.goto('/staged')
  const panel = page.getByLabel('Cleanup agent', { exact: true })
  await expect(panel.locator('strong')).toHaveText('Cleanup agent disconnected')
  await expect(panel.locator('p')).toHaveText('New requests are paused; your items remain staged. Open disky on the laptop and check its network connection, then check again. A computer restart should not be necessary.')
  await expect(panel.locator('time')).toHaveAttribute('datetime', new Date((NOW - 45391) * 1000).toISOString())
  await expect(page.locator('.dispatch button.dry')).toBeDisabled()
  await expect(page.locator('.dispatch button.danger')).toBeDisabled()
  await page.screenshot({ path: '../tmp/cleanup-agent-offline.png', fullPage: true })
  f.ready()
  await panel.getByRole('button', { name: 'check again', exact: true }).click()
  await expect(panel.locator('strong')).toHaveText('Cleanup agent connected')
  expect(f.dispatches).toEqual([])
  f.error()
  await panel.getByRole('button', { name: 'check again', exact: true }).click()
  await expect(panel.locator('strong')).toHaveText('Cleanup agent status unknown')
  await expect(panel.getByRole('alert')).toHaveText('Could not check the cleanup agent: Status service unavailable. Check Runs before retrying a submission.')
  expect(f.dispatches).toEqual([])
})

test('queued, running and partial completion remain distinct', async ({ page }) => {
  const f = await fixture(page, true)
  f.runs([RUN])
  await page.goto('/staged')
  await expect(page.locator('.rstate')).toHaveText('waiting for agent')
  f.runs([{ ...RUN, executor_started_ts: NOW - 30 }])
  await page.getByRole('button', { name: 'check again', exact: true }).click()
  await expect(page.locator('.rstate')).toHaveText('running')
  f.runs([{ ...RUN, executor_started_ts: NOW - 30, finished_ts: NOW, deleted_bytes: 4000000, deleted_objects: 5, execution_errors_json: JSON.stringify([[PREFIX, 'access denied']]) }])
  await page.getByRole('button', { name: 'check again', exact: true }).click()
  await expect(page.locator('.rstate')).toHaveText('completed with issues')
  await expect(page.locator('.run-outcome.real')).toHaveText('moved to Trash 4.00 M · 5')
  await page.locator('details.why summary').click()
  await expect(page.locator('details.why div')).toHaveText(`${PREFIX}: access denied`)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: '../tmp/cleanup-agent-mobile.png', fullPage: true })
  expect(f.dispatches).toEqual([])
})

test('a newly accepted dry request is shown once as waiting for the agent, without a stale submission notice', async ({ page }) => {
  const f = await fixture(page, true)
  await page.goto('/staged')
  await page.getByRole('checkbox', { name: 'select all', exact: true }).check()
  await page.getByRole('button', { name: 'dry-run 1 selected', exact: true }).click()
  await expect(page.locator('.rstate')).toHaveText('waiting for agent')
  await expect(page.locator('.dispatch-pending')).toHaveCount(0)
  expect(f.dispatches).toEqual([{ plan_id: 1, mode: 'dry', date: '2026-11-10T1200', items: [PREFIX] }])
})
