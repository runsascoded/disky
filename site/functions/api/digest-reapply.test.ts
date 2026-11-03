// `/api/digest-reapply` (specs/digest-runway-marks.md §4): what a request may name, the digest CLI arguments it
// becomes, and the Batch task's script.
import { describe, expect, it } from 'vitest'
import { parseReapply, reapplyArgs, reapplyScript } from './digest-reapply'

const base = { month: '2026-11', target: 'op', dry_run: true }

describe('/api/digest-reapply', () => {
  it('reads a target of the month, a channel (default the deployment’s) and candidate settings', () => {
    expect([
      parseReapply(base, 'C0C1YR7D0KU'),
      parseReapply({ ...base, channel: 'D0CE8QE83EH', target: '2026-11-01..2026-11-02', dry_run: false, params: 'qh,pp', quota_marks: 'runway-hue' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: 'thread' }, 'C0C1YR7D0KU'),
    ]).toEqual([
      { month: '2026-11', channel: 'C0C1YR7D0KU', target: 'op', dry_run: true, params: null, quota_marks: null },
      { month: '2026-11', channel: 'D0CE8QE83EH', target: '2026-11-01..2026-11-02', dry_run: false, params: 'qh,pp', quota_marks: 'runway-hue' },
      { month: '2026-11', channel: 'C0C1YR7D0KU', target: 'thread', dry_run: true, params: null, quota_marks: null },
    ])
    const T = 'target: thread | op | a day of 2026-11 | an ordered range of its days (A..B)'
    expect([
      parseReapply(base, undefined),
      parseReapply({ ...base, channel: 'general' }, undefined),
      parseReapply({ ...base, month: '2026-13' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-10-31' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-11-31' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-11-03..2026-11-02' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-11-01..2026-11-02..2026-11-03' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: "op'; rm -rf /" }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, dry_run: 'yes' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, params: 'qx' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, quota_marks: 'squares' }, 'C0C1YR7D0KU'),
    ]).toEqual([
      'channel: a Slack channel id (no DIGEST_CHANNEL default here)',
      'channel: a Slack channel id (no DIGEST_CHANNEL default here)',
      'month: YYYY-MM', T, T, T, T, T,
      'dry_run: a boolean',
      'params: digest params: unreadable field "qx" in "qx"',
      'quota_marks: warning | circles | runway | runway-hue',
    ])
  })

  it('becomes the digest CLI’s selectors', () => {
    const r = (o: object) => reapplyArgs({ month: '2026-11', channel: 'C1', target: 'op', dry_run: false, params: null, quota_marks: null, ...o })
    expect([
      r({}),
      r({ target: '2026-11-02', dry_run: true }),
      r({ target: '2026-11-01..2026-11-02', params: '', quota_marks: 'runway-hue' }),
      r({ target: 'thread', dry_run: true, params: 'qh,pp' }),
    ]).toEqual([
      ['-m', '2026-11', '-c', 'C1', '-o'],
      ['-m', '2026-11', '-c', 'C1', '-y', '2026-11-02..2026-11-02', '-n'],
      ['-m', '2026-11', '-c', 'C1', '-y', '2026-11-01..2026-11-02', '-p', '', '-q', 'runway-hue'],
      ['-m', '2026-11', '-c', 'C1', '-n', '-p', 'qh,pp'],
    ])
  })

  it('runs the CLI in the scan steps’ environment, its output teed to the run’s file', () => {
    expect(reapplyScript(['-m', '2026-11', '-c', 'C1', '-o', '-p', ''], '/gcs/data/digest/reapply/cw-digest-20261103-120000z.txt').split('\n')).toEqual([
      'set -euo pipefail',
      'cd "${APP_DIR:-/app}"',
      '. job/cw-env.sh',
      '{ set +x; } 2>/dev/null',
      "OUT='/gcs/data/digest/reapply/cw-digest-20261103-120000z.txt'",
      'mkdir -p "$(dirname "$OUT")"',
      `dt-cloud digest -T cw -C job/digest.yml -r "gs://$DATA_BUCKET/snapshots/cw" '-m' '2026-11' '-c' 'C1' '-o' '-p' '' 2>&1 | tee "$OUT"`,
    ])
  })
})
