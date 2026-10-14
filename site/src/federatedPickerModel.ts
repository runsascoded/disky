import { federatedEvents, resolveFederation, utcInstant, type SourceCatalog } from './federatedScans'

export interface CutoffDay {
  day: string
  events: { at: string; time: string; labels: string[] }[]
}

/** Viewer-local display only: values and URLs remain exact UTC instants. */
export function cutoffDays(catalogs: readonly SourceCatalog[], timeZone: string): CutoffDay[] {
  const labels = new Map(catalogs.map(c => [c.source, c.label]))
  const dayFmt = new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' })
  const timeFmt = new Intl.DateTimeFormat('en-GB', { timeZone, hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' })
  const groups = new Map<string, CutoffDay>()
  for (const e of federatedEvents(catalogs).reverse()) {
    const day = dayFmt.format(new Date(e.at))
    const g = groups.get(day) ?? { day, events: [] }
    g.events.push({ at: e.at, time: timeFmt.format(new Date(e.at)), labels: e.sources.map(s => labels.get(s)!) })
    groups.set(day, g)
  }
  return [...groups.values()]
}

export function adjacentCutoffs(catalogs: readonly SourceCatalog[], cutoff: string): { previous: string | null; next: string | null } {
  const at = utcInstant(cutoff)
  const events = federatedEvents(catalogs)
  return {
    previous: events.filter(e => e.at < at).at(-1)?.at ?? null,
    next: events.find(e => e.at > at)?.at ?? null,
  }
}

export function cutoffCoverage(catalogs: readonly SourceCatalog[], cutoff: string): { ready: number; total: number } {
  const view = resolveFederation(catalogs, cutoff)
  return { ready: view.sources.filter(s => s.state === 'ready').length, total: view.sources.length }
}
