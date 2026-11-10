import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { DiffRemainder } from './DiffRemainder'
import { diffRemainderParent, diffCellAction, followDiffCell } from './diffRows'

const render = (key: string, label: string, status: 'added' | 'filler' = 'added') => renderToStaticMarkup(createElement(DiffRemainder, {
  node: { key, label, status }, onDrill: () => {},
}))

describe('diff remainder inspection', () => {
  it('resolves the containing folder, never the synthetic display path', () => {
    expect([
      diffRemainderParent({ key: 'marin/checkpoints/(other)', label: '(other)', status: 'added' }),
      diffRemainderParent({ key: '(other)', label: '(other)', status: 'added' }),
      diffRemainderParent({ key: 'marin/__unchanged__', label: '(unchanged)', status: 'filler' }),
      diffRemainderParent({ key: 'marin/(experiment)', label: '(experiment)', status: 'added' }),
    ]).toEqual([['marin', 'checkpoints'], [], ['marin'], null])
  })

  it('lets real parenthesized directories and files navigate normally', () => {
    expect([
      diffCellAction({ label: '(experiment)', k: 'dir', status: 'added' }),
      diffCellAction({ label: '(notes).txt', k: 'file', status: 'added' }),
      render('marin/(experiment)', '(experiment)'),
    ]).toEqual(['drill', 'open', ''])
  })

  it('explains nested groups and offers the containing folder without changing the scan range', () => {
    expect(render('marin/checkpoints/(other)', '(other)')).toBe('<div class="diff-remainder"><p>Grouped entries omitted at this zoom level; this is not a folder named “(other)”.</p><button type="button">Open containing folder</button><span>Keeps the selected scan range.</span></div>')
  })

  it('offers the existing listing when already in the containing folder, with its scope explicit', () => {
    expect(render('(other)', '(other)')).toBe('<div class="diff-remainder"><p>Grouped entries omitted at this zoom level; this is not a folder named “(other)”.</p><button type="button">View folder listing</button><span>The listing shows the after-scan contents, including entries outside this group.</span></div>')
  })

  it('identifies an unchanged filler separately', () => {
    expect(render('marin/__unchanged__', '(unchanged)', 'filler')).toBe('<div class="diff-remainder"><p>Unchanged entries omitted from this view.</p><button type="button">Open containing folder</button><span>Keeps the selected scan range.</span></div>')
  })
})


describe('direct diff cell activation', () => {
  it('opens a group’s real parent on a normal click and leaves ⌥-click to pin', () => {
    const calls: unknown[] = []
    const handlers = { drill: (segs: string[]) => calls.push(['drill', segs]), open: (segs: string[]) => calls.push(['open', segs]), listing: () => calls.push(['listing']) }
    const group = { key: 'run/checkpoints/(other)', label: '(other)', status: 'added' as const, k: 'dir' as const }
    expect([followDiffCell(group, false, handlers), followDiffCell(group, true, handlers)]).toEqual([true, false])
    expect(calls).toEqual([['drill', ['run', 'checkpoints']]])
  })

  it('opens the existing listing at the current folder and preserves normal directory and object actions', () => {
    const calls: unknown[] = []
    const handlers = { drill: (segs: string[]) => calls.push(['drill', segs]), open: (segs: string[]) => calls.push(['open', segs]), listing: () => calls.push(['listing']) }
    const group = { key: '(other)', label: '(other)', status: 'added' as const, k: 'dir' as const }
    expect([
      followDiffCell(group, false, handlers),
      followDiffCell({ ...group, key: 'run', label: 'run' }, false, handlers),
      followDiffCell({ ...group, key: 'run/model.bin', label: 'model.bin', k: 'file' }, false, handlers),
      followDiffCell({ ...group, status: 'root' }, false, handlers),
    ]).toEqual([true, true, true, false])
    expect(calls).toEqual([['listing'], ['drill', ['run']], ['open', ['run', 'model.bin']]])
  })
})
