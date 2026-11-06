import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MutationObserver, QueryClient, QueryClientProvider, type QueryObserverOptions } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EXEC_JOBS_KEY, JOBS_REFRESH_MS, runActionOptions, usePrefixHistory } from './plans'
import { QUERY_DEFAULTS } from './queryDefaults'
import { EXEC_CAPS, joinRuns, runControls, type DeletionRun, type ExecJob } from './runs'

vi.mock('./stores', () => ({ DEFAULT_STORE: { key: 'cw', executor: 'plan-sweep' } }))

it('loads CW per-prefix deletion outcomes even when its executor does not expose run files', () => {
  const client = new QueryClient()
  function History() { usePrefixHistory(2, false); return null }
  renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(History)))
  const options = client.getQueryCache().find({ queryKey: ['prefix-history', 2] })?.options as QueryObserverOptions | undefined
  expect(options?.enabled).toBe(true)
  client.clear()
})

describe('a run control (stop / undo / purge) and the jobs list its feedback comes from', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

  const RUN: DeletionRun = { run_id: 'r1', plan_id: 1, mode: 'real', started_ts: 100, finished_ts: 200, deleted_bytes: 1, deleted_objects: 1, skipped_gone: 0, undo_deadline: 900, undo_state: 'none' }
  const UNDO: ExecJob = { job_id: 'undo-1', op: 'undo', target: 'r1', state: 'QUEUED' }
  const NONE = { jobs: [] as ExecJob[], configured: true }
  /** The app's client (`main.tsx`): it holds a list fresh for a minute (a read it would skip is the one that matters), and
   *  asks a failed read twice more unless the read says otherwise. */
  const appClient = () => new QueryClient({ defaultOptions: QUERY_DEFAULTS })
  const deferred = <T,>() => { let resolve!: (v: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
  /** The undo control of `RUN`, by the jobs the page holds: `[offered, undoing]`. */
  const control = (client: QueryClient): [boolean, boolean] => {
    const c = runControls(joinRuns([RUN], client.getQueryData<typeof NONE>(EXEC_JOBS_KEY)!.jobs)[0], EXEC_CAPS['plan-sweep'], true, 300)
    return [c.undo, c.undoing]
  }
  /** A request's abort, as `fetch` reports it: a rejection with the signal's reason. */
  const aborted = (signal?: AbortSignal | null) => new Promise<never>((_, reject) => signal?.addEventListener('abort', () => reject(signal.reason)))
  /** A backend whose jobs list gains the undo's job when the undo is posted; each call as `[url, method, cache]`. `jobsGate`:
   *  what a jobs read waits for (or its abort, whichever is first). */
  const backend = (jobsGate: Promise<unknown> = Promise.resolve(), jobsStatus = 200) => {
    const calls: [string, string, RequestCache | undefined][] = []
    let jobs: ExecJob[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push([url, init.method!, init.cache])
      if (init.method === 'POST') { jobs = [UNDO]; return new Response(JSON.stringify({ ok: true, job_id: UNDO.job_id })) }
      await Promise.race([jobsGate, aborted(init.signal)])
      return new Response(JSON.stringify(jobsStatus === 200 ? { jobs, configured: true } : { error: 'batch list failed' }), { status: jobsStatus })
    })
    return calls
  }

  it('stays pending until the jobs list is read again past the browser\'s copy, so the control is never offered in between', async () => {
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    const gate = deferred<void>()
    const calls = backend(gate.promise)
    const action = new MutationObserver(client, runActionOptions(client))
    const done = action.mutate({ action: 'undo', run_id: 'r1' })
    await vi.waitFor(() => expect(calls).toEqual([['/api/plan-sweep/undo', 'POST', undefined], ['/api/plan-sweep/jobs', 'GET', 'reload']]))
    // The undo is submitted and the fresh list not in yet: still pending (the page's `busy`), on the list as it was.
    expect([action.getCurrentResult().isPending, control(client)]).toEqual([true, [true, false]])
    gate.resolve()
    await done
    expect([action.getCurrentResult().isPending, client.getQueryData(EXEC_JOBS_KEY), control(client)]).toEqual([false, { jobs: [UNDO], configured: true }, [false, true]])
    client.clear()
  })

  it('a jobs poll already in flight, answered from before the job existed, is not what the page ends on', async () => {
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    const stale = deferred<typeof NONE>()
    const poll = client.fetchQuery({ queryKey: EXEC_JOBS_KEY, queryFn: () => stale.promise, staleTime: 0 }).catch(() => undefined)
    const calls = backend()
    await new MutationObserver(client, runActionOptions(client)).mutate({ action: 'undo', run_id: 'r1' })
    // The poll's answer arrives after the fresh one: too late to count.
    stale.resolve(NONE)
    await poll
    expect([calls, client.getQueryData(EXEC_JOBS_KEY)]).toEqual([
      [['/api/plan-sweep/undo', 'POST', undefined], ['/api/plan-sweep/jobs', 'GET', 'reload']],
      { jobs: [UNDO], configured: true },
    ])
    client.clear()
  })

  it('a jobs list that can\'t be read fails the list, not the control that landed, and is asked once', async () => {
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    const calls = backend(Promise.resolve(), 500)
    const action = new MutationObserver(client, runActionOptions(client))
    await action.mutate({ action: 'undo', run_id: 'r1' })
    expect([action.getCurrentResult().status, client.getQueryState(EXEC_JOBS_KEY)?.error?.message, calls]).toEqual([
      'success', 'batch list failed', [['/api/plan-sweep/undo', 'POST', undefined], ['/api/plan-sweep/jobs', 'GET', 'reload']],
    ])
    client.clear()
  })

  it('a jobs list that never answers is given up on after 15 s: the control settles, the list carries the failure', async () => {
    vi.useFakeTimers()
    const client = appClient()
    client.setQueryData(EXEC_JOBS_KEY, NONE)
    const calls = backend(new Promise(() => {}))
    const action = new MutationObserver(client, runActionOptions(client))
    void action.mutate({ action: 'undo', run_id: 'r1' })
    await vi.advanceTimersByTimeAsync(JOBS_REFRESH_MS - 1)
    const before = action.getCurrentResult().status
    await vi.advanceTimersByTimeAsync(1)
    // One read, not the app default's three: the control is back at 15 s, on the list as the page held it.
    expect([JOBS_REFRESH_MS, before, action.getCurrentResult().status, client.getQueryState(EXEC_JOBS_KEY)?.error?.message, client.getQueryData(EXEC_JOBS_KEY), calls]).toEqual([
      15_000, 'pending', 'success', 'the jobs list didn’t answer in 15 s', NONE,
      [['/api/plan-sweep/undo', 'POST', undefined], ['/api/plan-sweep/jobs', 'GET', 'reload']],
    ])
    client.clear()
  })
})
