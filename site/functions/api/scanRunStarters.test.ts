import { describe, expect, it } from 'vitest'
import { withoutStarters, withStarterEmoji, type ScanRunRows } from '../_lib/scanRuns.js'
import type { ScanRun } from '../../src/scanRunsModel.js'

const run = (run_id: string, trigger: 'schedule' | 'manual', trigger_by: string | null) =>
  ({ run_id, scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger, trigger_by }) as unknown as ScanRun
const rows: ScanRunRows = { runs: [run('a', 'manual', 'ada@example.org'), run('b', 'schedule', null)], phases: [], outputs: [] }

describe('withoutStarters', () => {
  it('keeps a manual run’s starter for a signed-in viewer and a grant', () => {
    expect(withoutStarters(rows, { via: 'session' })).toBe(rows)
    expect(withoutStarters(rows, { via: 'grant' })).toBe(rows)
  })
  it('drops every run’s starter for a public viewer, and nothing else', () => {
    expect(withoutStarters(rows, { via: 'public' })).toEqual({
      runs: [
        { run_id: 'a', scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger: 'manual' },
        { run_id: 'b', scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger: 'schedule' },
      ],
      phases: [],
      outputs: [],
    })
  })
  it('passes a D1 without the tables through', () => {
    expect(withoutStarters(null, { via: 'public' })).toBe(null)
  })
})

describe('withStarterEmoji', () => {
  const more: ScanRunRows = {
    ...rows,
    runs: [run('a', 'manual', 'Ada@Example.org'), run('b', 'schedule', null), run('c', 'manual', 'bob@example.org'), run('d', 'manual', 'claude (disky-r2)'), run('e', 'manual', 'ada@example.org')],
  }
  it('asks once for every manual run’s address starter (lower-cased), and passes only a bare `:name:` on', async () => {
    const asked: string[][] = []
    const out = await withStarterEmoji(more, async emails => {
      asked.push([...emails])
      return { 'ada@example.org': ':ada:', 'bob@example.org': '<!channel>' }
    })
    expect(asked).toEqual([['ada@example.org', 'bob@example.org']])
    expect(out!.runs.map(r => [r.run_id, r.trigger_emoji ?? null])).toEqual([['a', ':ada:'], ['b', null], ['c', null], ['d', null], ['e', ':ada:']])
  })
  it('a failed lookup leaves the rows as they are; no starter, no ask', async () => {
    expect(await withStarterEmoji(more, async () => { throw new Error('slack-emoji lookup failed (429)') })).toBe(more)
    expect(await withStarterEmoji(rows, async () => { throw new Error('not asked') })).not.toBe(null)
  })
})
