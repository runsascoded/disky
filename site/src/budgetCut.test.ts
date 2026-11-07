import { describe, expect, it } from 'vitest'
import { BUDGET_CUT_REFETCH_MS, BUDGET_CUT_REFETCHES, budgetCutRefetch } from './budgetCut'

describe('budgetCutRefetch', () => {
  it('re-asks a budget-cut answer until it is whole, at most `BUDGET_CUT_REFETCHES` times; a whole one never', () => {
    expect([
      budgetCutRefetch(undefined, 0),
      budgetCutRefetch({}, 1),
      budgetCutRefetch({ budgetCut: true }, 1),
      budgetCutRefetch({ budgetCut: true }, BUDGET_CUT_REFETCHES),
      budgetCutRefetch({ budgetCut: true }, BUDGET_CUT_REFETCHES + 1),
    ]).toEqual([false, false, BUDGET_CUT_REFETCH_MS, BUDGET_CUT_REFETCH_MS, false])
  })
})
