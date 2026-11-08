import { useState } from 'react'
import { useUrlState } from 'use-prms'
import { ScanDurationChart } from '../ScansPage'
import { inSpan, runSpanParam } from '../runChart'
import { summarize, type ScanRun, type ScanRunPhase } from '../scanRunsModel'
import { colorer, fmtDur } from '../scanRunsUi'

// Fictional capture/queue/processing spans: a normal series, one long run and one four-day queue.
const runs: ScanRun[] = []
const phases: ScanRunPhase[] = []
for (let i = 0; i < 76; i++) {
  const at = Date.UTC(2026, 9, 1, 6) / 1000 + i * 12 * 3600
  const id = `example-${i}`
  const queue = i === 67 ? 96 * 3600 : i === 20 ? 14 * 3600 : 600
  const total = i === 67 ? 98 * 3600 : i === 20 ? 15 * 3600 : (1 + i % 4 / 2) * 3600
  runs.push({ run_id: id, store: 'primary', scan: new Date(at * 1000).toISOString().slice(0, 16).replace(':', ''),
    kind: 'ingest', parent: null, status: i % 13 === 0 ? 'failed' : 'succeeded', failed_phase: null, error: null,
    started_ts: at, finished_ts: at + total, job_name: null, region: null, machine: null, spot: null, tasks: null,
    image: null, cost_usd: null, gen: null, source: 'backfill', updated_ts: at + total })
  for (const [seq, [phase, start, end]] of ([['capture', 0, 360], ['queue', 360, queue], ['index', queue, total]] as const).entries()) {
    phases.push({ run_id: id, phase, seq, started_ts: at + start, finished_ts: at + end, status: 'done', note: null })
  }
}
const summaries = summarize(runs, phases, [], Date.UTC(2026, 10, 8) / 1000)

export default function ScanDurationsPreview() {
  const [span, setSpan] = useUrlState('w', runSpanParam)
  const [hovered, setHovered] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const current = summaries.find(s => s.run.run_id === hovered)
  return <main className="staged-page scans-page">
    <h1>Scan-duration chart preview</h1>
    <p>Development-only, fictional runs. Zooming frames the chart; selecting on the chart filters the rows.</p>
    <ScanDurationChart runs={summaries} color={colorer(['capture', 'queue', 'index'])} onPick={setPicked} win={span} onWin={setSpan}
      hover={{ id: hovered, enter: setHovered, leave: () => setHovered(null) }} />
    <p role="status" aria-label="Selected runs">{summaries.filter(s => inSpan(span, s.run.started_ts)).length} / {summaries.length} runs selected</p>
    <p aria-live="polite">{current ? `${current.run.scan}: ${fmtDur(current.secs!)} total` : 'Hover a bar for its real duration.'}</p>
    {picked && <p>Opened {picked}</p>}
  </main>
}
