import { readFileSync } from 'node:fs'
import { aggregate, diffAggs, renderDiff, type BenchFile } from './lib.ts'

// Two bench files → per (widget, key, phase) Δ ms and Δ % of the p50s.
//
//   pnpm bench:diff -- a.json b.json
//
// `a` is the baseline, `b` the candidate: a positive Δ is slower.

const [a, b] = process.argv.slice(2)
if (!a || !b) {
  console.error('usage: pnpm bench:diff -- <a.json> <b.json>')
  process.exit(2)
}
const load = (p: string): BenchFile => JSON.parse(readFileSync(p, 'utf8'))
const fa = load(a), fb = load(b)
const describe = (f: BenchFile) => `${f.base} · ${f.paths.join(' ')} · ${f.runs} run${f.runs > 1 ? 's' : ''} · ${f.cold ? 'cold' : 'warm'} · ${f.stamp}${f.note ? ` · ${f.note}` : ''}`
console.log(`a: ${describe(fa)}`)
console.log(`b: ${describe(fb)}`)
console.log()
console.log(renderDiff(diffAggs(aggregate(fa.results), aggregate(fb.results))))
