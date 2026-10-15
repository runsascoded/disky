import { latestObserved, utcInstant, type Observation } from './federatedScans'

/** How a source is marked in the picker and on the map: its icon (a URL or a site path) when it has one, else
 *  its label; the label is the icon's accessible name and tooltip. */
export interface SourceMarkInfo { source: string; label: string; icon?: string }

/** One picker source: its mark and its scans (a `SourceCatalog` is one; so is a union member's indexed scans,
 *  `/api/union-scans`). `observed_from` is a capture window's start, where the source records one; `day`: the
 *  scan names a calendar day, not an instant (a date-only id with no recorded start: its UTC midnight stands in,
 *  so it reads as its date, never as a viewer-local time of day). */
export interface PickerSource extends SourceMarkInfo {
  scans: readonly (Observation & { observed_from?: string; day?: true })[]
}

export interface CutoffDay {
  day: string
  events: { at: string; time: string; sources: SourceMarkInfo[]; day?: true }[]
}

const compare = (a: string, b: string): number => a < b ? -1 : a > b ? 1 : 0

/** A source's mark alone (its scans dropped). */
export const markOf = ({ source, label, icon }: SourceMarkInfo): SourceMarkInfo => ({ source, label, ...(icon ? { icon } : {}) })

/** Each distinct instant any source observed through, ascending, with the sources observed then (sorted); `day`
 *  when every observation there names a calendar day (`PickerSource`). */
export function pickerEvents(sources: readonly PickerSource[]): { at: string; sources: string[]; day?: true }[] {
  const times = new Map<string, { ids: Set<string>; day: boolean }>()
  for (const s of sources) for (const o of s.scans) {
    const e = times.get(o.observed_through) ?? { ids: new Set<string>(), day: true }
    e.ids.add(s.source)
    e.day &&= !!o.day
    times.set(o.observed_through, e)
  }
  return [...times].sort(([a], [b]) => compare(a, b)).map(([at, e]) => ({ at, sources: [...e.ids].sort(), ...(e.day ? { day: true as const } : {}) }))
}

/** A cutoff as the picker shows it: month, day, hour and minute in the viewer's zone, never seconds (the exact
 *  UTC instant stays in the exact-cutoff field and the URL); a calendar-day one (`day`) is its date alone. */
export function fmtCutoff(at: string, timeZone: string, day?: boolean): string {
  return day
    ? new Date(at).toLocaleDateString('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' })
    : new Date(at).toLocaleString('en-US', { timeZone, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

/** The cutoffs that name calendar days (every observation there a `day` one): shown as dates. */
export const dayCutoffs = (sources: readonly PickerSource[]): ReadonlySet<string> =>
  new Set(pickerEvents(sources).filter(e => e.day).map(e => e.at))

/** Viewer-local display only: values and URLs remain exact UTC instants. Chip times are `HH:MM`; a calendar-day
 *  cutoff groups under its own date and reads as it ("Oct 14"). Days newest first. */
export function cutoffDays(sources: readonly PickerSource[], timeZone: string): CutoffDay[] {
  const marks = new Map(sources.map(s => [s.source, markOf(s)]))
  const dayFmt = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' })
  const timeFmt = new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
  const groups = new Map<string, CutoffDay>()
  for (const e of pickerEvents(sources).reverse()) {
    const day = e.day ? e.at.slice(0, 10) : dayFmt.format(new Date(e.at))
    const g = groups.get(day) ?? { day, events: [] }
    g.events.push({ at: e.at, time: e.day ? fmtCutoff(e.at, timeZone, true) : timeFmt.format(new Date(e.at)), sources: e.sources.map(s => marks.get(s)!), ...(e.day ? { day: true as const } : {}) })
    groups.set(day, g)
  }
  return [...groups.values()].sort((a, b) => compare(b.day, a.day))
}

export function adjacentCutoffs(sources: readonly PickerSource[], cutoff: string): { previous: string | null; next: string | null } {
  const at = utcInstant(cutoff)
  const events = pickerEvents(sources)
  return {
    previous: events.filter(e => e.at < at).at(-1)?.at ?? null,
    next: events.find(e => e.at > at)?.at ?? null,
  }
}

/** Each source's scan at a cutoff (`latestObserved`: null = unknown, never a later one), sources by label. */
export function sourcesAt<S extends PickerSource>(sources: readonly S[], cutoff: string): { source: S; scan: S['scans'][number] | null }[] {
  const at = utcInstant(cutoff)
  return [...sources].sort((a, b) => compare(a.source, b.source)).map(source => ({ source, scan: latestObserved(source.scans, at) }))
}

export function cutoffCoverage(sources: readonly PickerSource[], cutoff: string): { ready: number; total: number } {
  const at = sourcesAt(sources, cutoff)
  return { ready: at.filter(s => s.scan).length, total: at.length }
}
