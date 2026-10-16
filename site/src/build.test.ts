import { describe, expect, it } from 'vitest'
import { buildLine } from './build'

describe('the ☰ menu\'s build line', () => {
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
