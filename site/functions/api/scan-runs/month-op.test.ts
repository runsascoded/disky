/** `POST /api/scan-runs/month-op` (specs/admin-monthly-thread.md): the monthly admin thread's opening post over a D1 of
 *  scan runs, with the status card's signed URL; and what it refuses. */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { deployData, deployVersion, type DeployEnv } from '../../_lib/og/deployData'
import { ogKey, resolveImage } from '../../_lib/og/sign'
import { sqliteD1 } from '../../_lib/testD1'
import { onRequestPost, parseMonthOpRequest } from './month-op'

// The card renderer's wasm (`og/serve.ts`, which the route imports for the deployment's key): nothing here draws.
vi.mock('../../_lib/og/vendor/resvg.wasm', () => ({ default: new Uint8Array() }))

const T = (iso: string) => Date.parse(iso) / 1000
/** 2026-11-24 12:00 UTC. */
const NOW = T('2026-11-24T12:00:00Z')
const ORIGIN = 'http://localhost'

/** October's clean run, then November's: a failure, a run whose `digest` step failed, two clean runs and one still
 *  running; a no-op and another store's run, which the post leaves out. */
const SEED = `
INSERT INTO scan_runs (run_id, store, scan, kind, parent, status, failed_phase, started_ts, finished_ts, cost_usd, source, updated_ts, trigger) VALUES
  ('o2', 'primary', '2026-10-30T0700', 'scan', NULL, 'succeeded', NULL, ${T('2026-10-30T07:00:00Z')}, ${T('2026-10-30T07:00:00Z') + 2400}, 0.40, 'job', 0, 'schedule'),
  ('n1', 'primary', '2026-11-02T0700', 'scan', NULL, 'failed', 'list', ${T('2026-11-02T07:00:00Z')}, ${T('2026-11-02T07:00:00Z') + 600}, 0.10, 'job', 0, 'schedule'),
  ('n2', 'primary', '2026-11-12T0700', 'scan', NULL, 'succeeded', NULL, ${T('2026-11-12T07:00:00Z')}, ${T('2026-11-12T07:00:00Z') + 2700}, 0.30, 'job', 0, 'manual'),
  ('n3', 'primary', '2026-11-20T0700', 'scan', NULL, 'succeeded', NULL, ${T('2026-11-20T07:00:00Z')}, ${T('2026-11-20T07:00:00Z') + 2800}, 0.25, 'job', 0, 'schedule'),
  ('n4', 'primary', '2026-11-23T0717', 'scan', NULL, 'succeeded', NULL, ${T('2026-11-23T07:17:00Z')}, ${T('2026-11-23T07:17:00Z') + 2780}, 0.17, 'job', 0, 'manual'),
  ('n5', 'primary', '2026-11-24T1100', 'scan', NULL, 'running', NULL, ${T('2026-11-24T11:00:00Z')}, NULL, NULL, 'job', ${NOW - 60}, 'schedule'),
  ('nop', 'primary', '2026-11-21T1800', 'scan', NULL, 'nop', NULL, ${T('2026-11-21T18:00:00Z')}, ${T('2026-11-21T18:00:05Z')}, NULL, 'job', 0, 'schedule'),
  ('m1', 'meta', '2026-11-23T0800', 'scan', NULL, 'failed', 'list', ${T('2026-11-23T08:00:00Z')}, ${T('2026-11-23T08:10:00Z')}, 0.01, 'job', 0, 'schedule');
INSERT INTO scan_run_phases (run_id, phase, seq, started_ts, finished_ts, status) VALUES
  ('n2', 'list b1', 0, ${T('2026-11-12T07:00:00Z')}, ${T('2026-11-12T07:30:00Z')}, 'done'),
  ('n2', 'digest', 1, ${T('2026-11-12T07:30:00Z')}, ${T('2026-11-12T07:45:00Z')}, 'failed');
`

async function seeded(extra: object = {}): Promise<DeployEnv> {
  const { db, raw } = await sqliteD1()
  raw.exec(SEED)
  // a meta store with no index store behind it: its series answers 503
  return { DB: db, DISKY_LABELS: 'app=disky,deployment=ops', STORES_JSON: JSON.stringify({ meta: {} }), ...extra } as DeployEnv
}

const post = (env: object, body: unknown, origin = ORIGIN) => onRequestPost({
  request: new Request(`${origin}/api/scan-runs/month-op`, { method: 'POST', body: JSON.stringify(body), headers: { 'content-type': 'application/json' } }),
  env,
} as never)

afterEach(() => { vi.restoreAllMocks() })

describe('POST /api/scan-runs/month-op', () => {
  it('renders the open month\'s post from D1, with the card\'s signed URL', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
    const env = await seeded({ SESSION_SECRET: 'secret' })
    const res = await post(env, { month: '2026-11', prev_url: 'https://slack.example.test/archives/C1/p1', color: '#EA4335' })
    const body = await res.json() as { text: string; card_url: string }
    const v = await deployVersion(await deployData(env, ORIGIN, NOW))
    expect([res.status, res.headers.get('cache-control'), body.text.split('\n'), body.card_url.replace(/&sig=\w+$/, '&sig=…')]).toEqual([
      200, 'no-store', [
        `✅ 2 consecutive clean runs · latest [11/23 07:17Z](${ORIGIN}/scans/n4) · 46m 20s · $0.17`,
        'This month: 5 runs · 2 clean · 1 with issues · 1 failed · 1 running · 3 scheduled / 2 manual',
        'Cost: $0.82 month-to-date · $1.22 last 30d · run time 47m (7d median, +3.3%)',
        `[Metadata](${ORIGIN}/meta): size unavailable (HTTP 503) · [All scans](${ORIGIN}/scans)`,
        '[← October](https://slack.example.test/archives/C1/p1)',
      ],
      `${ORIGIN}/og/deploy.png?c=ea4335&v=${v}&sig=…`,
    ])
    expect((await resolveImage(await ogKey('secret'), new URL(body.card_url), NOW))?.tier).toBe('full')
  })

  it('a closed month has its final figures and no card; no meta store, no Metadata', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
    const env = await seeded({ SESSION_SECRET: 'secret', STORES_JSON: undefined })
    const res = await post(env, { month: '2026-10', next_url: 'https://slack.example.test/archives/C1/p2' })
    expect([res.status, await res.json()]).toEqual([200, {
      text: [
        'October: 1 run · 1 clean · 1 scheduled / 0 manual',
        'Cost: $0.40 month total · $0.40 last 30d · run time 40m (7d median)',
        `[All scans](${ORIGIN}/scans)`,
        '[November →](https://slack.example.test/archives/C1/p2)',
      ].join('\n'),
      card_url: null,
    }])
  })

  it('refuses a malformed body, a month not started, and a deployment that can\'t sign or has no D1', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(NOW * 1000)
    const env = await seeded({ SESSION_SECRET: 'secret' })
    const answers = await Promise.all([
      post(env, { month: '2026-13' }),
      post(env, { month: '2026-12' }),
      post(env, { month: '2026-11', prev_url: 'javascript:alert(1)' }),
      post(env, { month: '2026-11', emoji: { clean: 'ok' } }),
      post({ ...env, SESSION_SECRET: undefined }, { month: '2026-11' }),
      post({ SESSION_SECRET: 'secret' }, { month: '2026-11' }),
    ].map(async p => { const r = await p; return [r.status, await r.json()] }))
    expect(answers).toEqual([
      [400, { error: 'month: YYYY-MM' }],
      [400, { error: 'month 2026-12 hasn\'t started' }],
      [400, { error: 'prev_url: an https URL' }],
      [400, { error: 'emoji: {outcome: a Slack :name: code or emoji characters}, outcomes clean, issues, failed, cancelled, running' }],
      [503, { error: 'card URLs not configured (SESSION_SECRET unset)' }],
      [503, { error: 'no D1 bound' }],
    ])
  })

  it('is the job\'s grant\'s: a caller with no credential is refused before anything is read', async () => {
    const res = await post(await seeded({ SESSION_SECRET: 'secret' }), { month: '2026-11' }, 'https://ops.example.test')
    expect([res.status, await res.json()]).toEqual([401, { error: 'unauthenticated' }])
  })

  it('parses the body: links, a colour, outcome marks', () => {
    expect(parseMonthOpRequest({ month: '2026-11', next_url: 'https://x.test/p', color: 'EA4335', emoji: { failed: ':boom:', clean: '🟢' } })).toEqual({
      month: '2026-11', prevUrl: null, nextUrl: 'https://x.test/p', color: 'ea4335', marks: { failed: ':boom:', clean: '🟢' },
    })
    expect([parseMonthOpRequest(null), parseMonthOpRequest({ month: '2026-11', color: 'red' }), parseMonthOpRequest({ month: '2026-11', next_url: 'https://x.test/a)b' })]).toEqual([
      'body: a JSON object', 'color: #rrggbb', 'next_url: an https URL',
    ])
  })
})
