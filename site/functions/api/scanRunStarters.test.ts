import { describe, expect, it } from 'vitest'
import { withoutStarters, withStarterEmoji, type ScanRunRows } from '../_lib/scanRuns.js'
import { sqliteD1 } from '../_lib/testD1'
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
  it('drops the starter’s emoji too', () => {
    const withEmoji = { ...rows, runs: [{ ...rows.runs[0], trigger_emoji: ':ada:' }] }
    expect(withoutStarters(withEmoji, { via: 'public' })!.runs).toEqual([
      { run_id: 'a', scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger: 'manual' },
    ])
  })
  it('passes a D1 without the tables through', () => {
    expect(withoutStarters(null, { via: 'public' })).toBe(null)
  })
})

describe('withStarterEmoji', () => {
  it('joins a manual run’s starter (case-insensitively) to their profile’s Slack emoji; others unchanged', async () => {
    const { db, raw } = await sqliteD1()
    raw.prepare("INSERT INTO profiles (email, name, avatar, avatar_src, updated_at, slack_emoji) VALUES ('ada@example.org', NULL, NULL, NULL, 1, ':ada:')").run()
    raw.prepare("INSERT INTO profiles (email, name, avatar, avatar_src, updated_at, slack_emoji) VALUES ('bob@example.org', NULL, NULL, NULL, 1, '<!channel>')").run()
    const more: ScanRunRows = { ...rows, runs: [run('a', 'manual', 'Ada@Example.org'), run('b', 'schedule', null), run('c', 'manual', 'bob@example.org'), run('d', 'manual', 'eve@example.org')] }
    expect((await withStarterEmoji(db, more))!.runs.map(r => [r.run_id, r.trigger_emoji ?? null])).toEqual([
      ['a', ':ada:'], ['b', null], ['c', null], ['d', null],
    ])
  })
  it('a database without the column (auth’s 0007 unapplied) leaves the rows as they are', async () => {
    const { db, raw } = await sqliteD1()
    raw.exec('ALTER TABLE profiles DROP COLUMN slack_emoji')
    expect(await withStarterEmoji(db, rows)).toBe(rows)
  })
})
