import { describe, expect, it } from 'vitest'
import { buildLine, sourceUrl } from './build'

describe('the ☰ menu\'s build line', () => {
  it('links the deployment branch separately from the running commit', () => {
    const repo = 'https://github.com/Open-Athena/disky/'
    expect(['gcs', 'cw', 'hf', 'r2', 'work/hf/size chart', ''].map(ref => sourceUrl(repo, ref))).toEqual([
      'https://github.com/Open-Athena/disky/tree/gcs',
      'https://github.com/Open-Athena/disky/tree/cw',
      'https://github.com/Open-Athena/disky/tree/hf',
      'https://github.com/Open-Athena/disky/tree/r2',
      'https://github.com/Open-Athena/disky/tree/work/hf/size%20chart',
      'https://github.com/Open-Athena/disky',
    ])
    expect(buildLine(repo, '1234567890abcdef1234567890abcdef12345678', '')).toEqual({
      sha: '12345678', time: '', href: 'https://github.com/Open-Athena/disky/commit/1234567890abcdef1234567890abcdef12345678',
    })
  })
  it('a clean build links its commit; a dirty or unknown one names itself, unlinked; none, no line', () => {
    const repo = 'https://github.com/runsascoded/disky'
    expect([
      buildLine(repo, '8199b3c0ab', '2026-10-15T23:59:01Z'),
      buildLine(`${repo}/`, '8199b3c0ab-dirty', '2026-10-15T23:59:01Z'),
      buildLine(repo, 'unknown', '2026-10-15T23:59:01Z'),
      buildLine(repo, '', ''),
    ]).toEqual([
      { sha: '8199b3c0', time: '2026-10-15 23:59 UTC', href: 'https://github.com/runsascoded/disky/commit/8199b3c0ab' },
      { sha: '8199b3c0ab-dirty', time: '2026-10-15 23:59 UTC', href: null },
      { sha: 'unknown', time: '2026-10-15 23:59 UTC', href: null },
      null,
    ])
  })
})
