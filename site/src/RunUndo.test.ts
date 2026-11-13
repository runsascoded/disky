import { createElement, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { RecoveryUnavailable, RecoveryUnseen, UndoBy, UndoControl, UndoLine, undoUnavailableTip, undoUnseenTip } from './RunUndo'
import { type DeletionRun, type UndoStatus, undoStatus } from './runs'
import { runUndoUnavailable, runUndoUnseen } from './softDelete'

const strip = (html: string): string => html.replace(/<[^>]*>/g, '').replace(/&#x27;/g, "'")
const text = (el: ReactElement): string => strip(renderToStaticMarkup(el))
const off = { buckets: ['b1'], asOf: '2026-11-05' }
const unseen = { unseen: ['b1'] }
const STATUSES: UndoStatus[] = [
  { kind: 'unavailable', unavailable: off, deadline: 9000 },
  { kind: 'open', deadline: 9000, unseen },
  { kind: 'open', deadline: 9000 },
  { kind: 'closed', deadline: 9000 },
  { kind: 'none', why: 'No undo: a bucket in this run had soft delete off, so what it deleted there is gone for good.' },
  { kind: 'unrecorded' },
]

describe('the runs table\'s "undo by" cell', () => {
  it('reads unavailable where soft delete is off now, unverified where nothing says; else the time left, closed, none, or nothing recorded', () => {
    expect(STATUSES.map(status => text(createElement(UndoBy, { status, now: 2000 })))).toEqual(['unavailable', '1h 56m · unverified', '1h 56m', 'closed', 'none', '—'])
  })
  it('a reason a phone can reach: every cell that has one is a tip a tap pins', () => {
    expect(STATUSES.map(status => /class="tt-ref pinnable"/.test(renderToStaticMarkup(createElement(UndoBy, { status, now: 2000 }))))).toEqual([true, true, false, true, true, false])
  })
  it('an unverified deadline says what it depends on, and who does know', () => {
    expect(undoUnseenTip(unseen, 9000)).toBe("Recorded deadline: 1970-01-01 02:30 UTC. Undo depends on soft delete still being on for b1, which this site can't see. The undo job checks the bucket itself, and reports anything it can't restore.")
  })
  it('its tooltip names the buckets and the snapshot\'s date, and keeps the deadline as a record', () => {
    expect(undoUnavailableTip(off, 9000)).toBe('Undo unavailable: soft delete is off on b1 (as of 2026-11-05). Undo restores from what the bucket keeps after a delete, so its soft delete (versioning, on S3) has to be on. Recorded deadline: 1970-01-01 02:30 UTC.')
  })
})

describe('an undone run\'s "undo by" cell, as the runs table composes it', () => {
  const run = (undo_state: string): DeletionRun => ({
    run_id: '2026-11-01-p1/20261101T120000Z', plan_id: 1, mode: 'real', scan: '2026-11-01', actor: 'ann', started_ts: 1000, finished_ts: 1500,
    deleted_bytes: 600, deleted_objects: 6, skipped_gone: 0, undo_deadline: 9000, undo_state, purge_state: 'none', buckets: 'b1',
  })
  const cell = (r: DeletionRun, known: { asOf: string; off: Record<string, boolean> } | null) =>
    text(createElement(UndoBy, { status: undoStatus(r, 2000, runUndoUnavailable(r, ['b1'], known, 2000), runUndoUnseen(r, ['b1'], known, 2000)), now: 2000 }))
  const offNow = { asOf: '2026-11-05', off: { b1: true } }
  it('says its recorded window and nothing of an undo it no longer needs; a run not undone, or partly, is still qualified', () => {
    expect([
      cell(run('full'), offNow), cell(run('full'), null),
      cell(run('none'), offNow), cell(run('none'), null),
      cell(run('partial'), offNow), cell(run('partial'), null),
    ]).toEqual(['1h 56m', '1h 56m', 'unavailable', '1h 56m · unverified', 'unavailable', '1h 56m · unverified'])
  })
})

describe('a run\'s detail: its undo line', () => {
  it('says undo is unavailable, with the bucket and the date, instead of counting down to a deadline', () => {
    expect(STATUSES.map(status => text(createElement(UndoLine, { status, now: 2000, live: false })))).toEqual([
      'unavailable: soft delete is off on b1 (as of 2026-11-05) · recorded run deadline 1970-01-01 02:30 UTC',
      "recorded run deadline 1970-01-01 02:30 UTC (in 1h 56m) · undo depends on soft delete still being on for b1, which this site can't see",
      'recorded run deadline 1970-01-01 02:30 UTC (in 1h 56m)',
      'recorded run deadline 1970-01-01 02:30 UTC (closed)',
      'none',
      'no window recorded',
    ])
    expect(text(createElement(UndoLine, { status: { kind: 'unrecorded' }, now: 2000, live: true }))).toBe('recovery after stop and drain; final deadline not recorded yet')
  })
})

describe('a run\'s detail: its recovery section, where soft delete is off now', () => {
  const parts = (buckets: string[], unavailable = off) => {
    const html = renderToStaticMarkup(createElement(RecoveryUnavailable, { unavailable, buckets, runId: '2026-11-01-p1/20261101T120000Z' }))
    return [...html.matchAll(/<p[^>]*>(.*?)<\/p>/g)].map(m => strip(m[1]))
  }
  it('every bucket off: no undo to offer, and where the snapshot\'s word comes from', () => {
    expect(parts(['b1'])).toEqual([
      'Undo unavailable: soft delete is off on b1 (as of 2026-11-05). Undo restores from what the bucket keeps after a delete, so its soft delete (versioning, on S3) has to be on. The web undo is disabled for this run.',
      'As the job last read it, after a scan. dt-cloud sweep undo asks GCS itself.',
    ])
  })
  it('mixed: the buckets still on can be restored from the CLI, by name', () => {
    expect(parts(['b2', 'b1', 'b3'], { buckets: ['b1'], asOf: '2026-11-05' })).toEqual([
      'Undo unavailable: soft delete is off on b1 (as of 2026-11-05). Undo restores from what the bucket keeps after a delete, so its soft delete (versioning, on S3) has to be on. The web undo is disabled for this run.',
      'As the job last read it, after a scan. dt-cloud sweep undo asks GCS itself.',
      'Not shown off: b2, b3. What the run deleted there can still be restored from the CLI, which asks GCS about each bucket first:',
      'dt-cloud sweep undo -b b2 -b b3 2026-11-01-p1/20261101T120000Z',
    ])
  })
})

describe('nothing says whether soft delete is still on where a run deleted', () => {
  it('its recovery section says what the undo depends on, without calling it unavailable', () => {
    expect(text(createElement(RecoveryUnseen, { unseen: { unseen: [] } }))).toBe("Undo depends on the buckets' soft delete, which this site can't see. The undo job checks the bucket itself, and reports anything it can't restore.")
  })
})

describe('the undo control', () => {
  const control = (off: string | null, busy = false) => renderToStaticMarkup(createElement(UndoControl, { off, tip: 'Restore what this run deleted.', busy, onUndo: () => {} }))
  it('offered: a plain button; unavailable: still there, aria-disabled, in a tip a tap pins (a disabled button could open none)', () => {
    expect([control(null), control(null, true), control('Already undone.')]).toEqual([
      '<span class="tt-ref" tabindex="0"><button type="button" class="undo">undo</button></span>',
      '<span class="tt-ref" tabindex="0"><button type="button" class="undo" disabled="">undo</button></span>',
      '<span class="tt-ref pinnable" tabindex="0"><button type="button" class="undo off" aria-disabled="true">undo</button></span>',
    ])
  })
})
