#!/usr/bin/env node
// Contract + perf harness for /api/subtree (specs/done/path-index-lazy-drill.md).
// The endpoint's contract is arithmetic, so assert it directly:
//   - every non-fold child's b ≥ the (attenuated) threshold at its depth
//   - Σ(children) + (other) ≈ parent, within the fold floor at that level
//   - node count ≤ w·h/minArea (+1 for the root)
// Usage: scripts/subtree-check.mjs <base-url> <scan date>
// The cases come from the deployment's own tree: the root, then down the
// largest child three levels (at two canvas sizes), so any store exercises
// its deepest heavy path.
const [base, date] = process.argv.slice(2)
if (!base || !date) { console.error('usage: subtree-check.mjs <base-url> <scan date>'); process.exit(2) }

async function largestChain(depth) {
  const chain = []
  let path = ''
  for (let i = 0; i < depth; i++) {
    const r = await fetch(`${base}/api/subtree?date=${date}&path=${encodeURIComponent(path)}&w=1600&h=900`)
    if (!r.ok) break
    const kids = ((await r.json()).tree.c ?? []).filter(k => !k.n.startsWith('(')).sort((a, b) => b.b - a.b)
    if (!kids.length) break
    path = path ? `${path}/${kids[0].n}` : kids[0].n
    chain.push(path)
  }
  return chain
}
const chain = await largestChain(3)
const CASES = [
  { path: '', w: 1600, h: 900 },
  ...chain.map(path => ({ path, w: 1600, h: 900 })),
  ...chain.slice(-1).map(path => ({ path, w: 1280, h: 800 })),
]

let failures = 0
const fail = (msg) => { failures++; console.error('  FAIL:', msg) }

for (const c of CASES) {
  const url = `${base}/api/subtree?date=${date}&path=${encodeURIComponent(c.path)}&w=${c.w}&h=${c.h}`
  const t0 = Date.now()
  const res = await fetch(url)
  const ms = Date.now() - t0
  if (!res.ok) { fail(`${c.path || '(root)'}: HTTP ${res.status}`); continue }
  const d = await res.json()
  const thrAt = (depth) => d.threshold * (d.atten ?? 2) ** Math.max(0, depth)
  let nodes = 0
  const walk = (n, depth) => {
    nodes++
    const kids = n.c ?? []
    if (!kids.length) return
    const thr = thrAt(depth)
    for (const k of kids) {
      if (!k.n.startsWith('(') && k.b < thr) fail(`${c.path}: child ${k.n} b=${k.b} < thr(${depth})=${thr.toFixed(0)}`)
      walk(k, depth + 1)
    }
    const sum = kids.reduce((s, k) => s + k.b, 0)
    if (Math.abs(sum - n.b) > thr + 2) fail(`${c.path}: Σchildren=${sum} vs parent ${n.b} (Δ>${thr.toFixed(0)})`)
  }
  walk(d.tree, 0)
  const budget = (d.w * d.h) / d.minArea + 1
  if (nodes > budget) fail(`${c.path}: ${nodes} nodes > budget ${budget}`)
  if (nodes !== d.nodes + (d.tier === 'fine' ? 1 : 0) && d.tier === 'fine')
    console.warn(`  note: counted ${nodes} vs reported ${d.nodes}`)
  console.log(
    `ok [${String(ms).padStart(5)}ms] ${c.path || '(root)'}: tier=${d.tier} nodes=${nodes} thr=${(d.threshold / 1e9).toFixed(2)}GB truncated=${d.truncated}`,
  )
}
if (failures) { console.error(`${failures} failure(s)`); process.exit(1) }
console.log('contract holds')
