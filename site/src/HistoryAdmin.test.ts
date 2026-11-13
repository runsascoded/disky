import { describe, expect, it } from 'vitest'
import { DEFAULT_HISTORY_DRAFT, geometricDescription, historyPolicyForPreview } from './HistoryAdmin'

const A = '2026-10-01T0000', B = '2026-10-02T0000'

describe('history policy drafts', () => {
  it('starts Off and produces no preview request even with selected scans and valid parameters', () => {
    expect(DEFAULT_HISTORY_DRAFT).toEqual({ kind: 'off', dropCount: 1, recentDays: 7, growth: 2, samples: 1 })
    expect(historyPolicyForPreview(DEFAULT_HISTORY_DRAFT, [A, B])).toBe(null)
  })

  it('creates a precise manual removal set from repeated scan rows and refuses empty or invalid selections', () => {
    const draft = { ...DEFAULT_HISTORY_DRAFT, kind: 'manual' as const }
    expect(historyPolicyForPreview(draft, [B, A, B])).toEqual({ kind: 'manual', remove: [A, B] })
    expect([historyPolicyForPreview(draft), historyPolicyForPreview(draft, ['2026-02-30'])]).toEqual([null, null])
  })

  it('sends all geometric parameters explicitly and explains the complete selected policy', () => {
    const draft = { ...DEFAULT_HISTORY_DRAFT, kind: 'geometric' as const, recentDays: 3, growth: 1.5, samples: 2 }
    expect(historyPolicyForPreview(draft)).toEqual({ kind: 'geometric', recent_seconds: 259200, growth: 1.5, samples_per_band: 2 })
    expect(geometricDescription(draft)).toBe('Keep every scan from the newest 3 days; grow older age bands by 1.5×; keep 2 samples per band.')
    expect(geometricDescription(DEFAULT_HISTORY_DRAFT)).toBe('Keep every scan from the newest 7 days; grow older age bands by 2×; keep 1 sample per band.')
  })

  it('does not create requests from invalid numeric drafts', () => {
    expect([
      historyPolicyForPreview({ ...DEFAULT_HISTORY_DRAFT, kind: 'low-change', dropCount: 1.5 }),
      historyPolicyForPreview({ ...DEFAULT_HISTORY_DRAFT, kind: 'geometric', growth: 1 }),
      historyPolicyForPreview({ ...DEFAULT_HISTORY_DRAFT, kind: 'geometric', samples: 0 }),
      historyPolicyForPreview({ ...DEFAULT_HISTORY_DRAFT, kind: 'geometric', recentDays: NaN }),
    ]).toEqual([null, null, null, null])
    expect(historyPolicyForPreview({ ...DEFAULT_HISTORY_DRAFT, kind: 'low-change', dropCount: 8 })).toEqual({ kind: 'low-change', drop_count: 8 })
    expect(geometricDescription({ ...DEFAULT_HISTORY_DRAFT, recentDays: NaN })).toBe('Choose the recent window, age-band growth and samples per band below.')
  })
})
