/** Real ordinary-process Node versus workerd/R2/D1. No vi.mock or Cloudflare-shaped globals in Node. */
import { deepStrictEqual, strictEqual } from 'node:assert'
import { spawnSync } from 'node:child_process'
import { readFile, mkdir, appendFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { Miniflare } from 'miniflare'
import { build } from 'esbuild'
import { captureFixture, fixtureRequests } from './fixtures.js'
import { sha256, nodeExecutor } from './node.js'

const site = resolve(process.argv[2] ?? '.')
const out = resolve(process.argv[3] ?? '../tmp/executor-parity')
const indexed = process.argv[4] === 'indexed'
await mkdir(out, { recursive: true })
await writeFile(`${out}/samples.jsonl`, '')
const document = await captureFixture(site, `${out}/capsule.json`)
if (indexed) {
  const { id: _id, ...capsule } = document.capsule
  capsule.config = { ...capsule.config, FILTER_INDEXED_ONLY: '1' }
  document.capsule = { id: sha256(JSON.stringify({ capsule, sql: document.sql, objects: document.objects })), ...capsule }
  await writeFile(`${out}/capsule.json`, JSON.stringify(document, null, 2) + '\n')
}
const bundle = await build({ entryPoints: [`${site}/executor/worker.ts`], bundle: true, format: 'esm', platform: 'neutral', conditions: ['workerd', 'worker', 'browser'], external: ['node:*'], write: false })
const mf = new Miniflare({
  modules: true,
  script: bundle.outputFiles[0].text,
  compatibilityDate: '2026-11-07',
  compatibilityFlags: ['nodejs_compat'],
  bindings: { CAPSULE: JSON.stringify(document.capsule) },
  r2Buckets: ['INDEX_R2'],
  d1Databases: ['DB'],
})
try {
  const db = await mf.getD1Database('DB'), r2 = await mf.getR2Bucket('INDEX_R2')
  await db.exec(document.sql)
  for (const object of document.objects) await r2.put(object.key, await readFile(resolve(site, object.file)))
  const node = await nodeExecutor(document, site)
  const normal = fixtureRequests(document.capsule)
  const base = normal.find(c => c.name === 'name')!.request
  if (base.kind !== 'map') throw new Error('fixture name case must be a map')
  const cases = indexed ? [
    { name: 'unsupported-slash', request: { ...base, options: { ...base.options, query: 'a/b' } } },
    { name: 'unsupported-scope', request: { ...base, options: { ...base.options, query: 'abc', lens: { key: 'alice' } } } },
    { name: 'scan-not-indexed', request: base },
  ] : normal
  let compared = 0
  for (const phase of ['cold', 'warm', 'concurrent'] as const) {
    const one = async ({ name, request }: typeof cases[number]) => {
      if (phase !== 'cold' && (name === 'gets-budget' || name === 'bytes-budget')) return
      const start = performance.now(), cpu = process.cpuUsage()
      const nodeResult = await node(request)
      const nodeCpu = process.cpuUsage(cpu)
      const expected = indexed || name === 'invalid-query' ? 'unsupported'
        : name.endsWith('-budget') || name === 'deadline' ? 'exhausted'
        : name === 'unavailable-scan' || name === 'wrong-capsule' ? 'unavailable' : 'answered'
      strictEqual(nodeResult.state, expected, `${phase}/${name}: Node outcome`)
      if (name === 'root-2026-07-30' && nodeResult.state === 'answered' && 'tree' in nodeResult.body) deepStrictEqual([nodeResult.body.tree.b, nodeResult.body.tree.o], [20, 3], 'golden root includes its zero-byte object')
      const response = await mf.dispatchFetch('https://capsule.invalid/run', { method: 'POST', body: JSON.stringify(request) })
      const workerResult = await response.json() as Record<string, unknown>
      strictEqual(response.status, 200, `${phase}/${name}: Worker failure ${JSON.stringify(workerResult)}`)
      const { telemetry: nt, ...na } = nodeResult as unknown as Record<string, unknown>
      const { telemetry: wt, ...wa } = workerResult
      // Only telemetry is excluded. The complete body, partial/refusal fields and pinned revisions must agree.
      deepStrictEqual(JSON.parse(JSON.stringify(na)), wa, `${phase}/${name}`)
      await appendFile(`${out}/samples.jsonl`, JSON.stringify({ phase, name, node: nt, worker: wt, completeMs: performance.now() - start, nodeCpu, nodeRss: process.memoryUsage().rss, workerMemory: null, aggregationCpu: null, workerCpu: null }) + '\n')
      compared++
      console.log(`${phase}/${name}: ${nodeResult.state}`)
    }
    if (phase === 'concurrent') {
      for (let i = 0; i < cases.length; i += 2) await Promise.all(cases.slice(i, i + 2).map(one))
    } else for (const entry of cases) await one(entry)
  }
  console.log(JSON.stringify({ compared, capsule: document.capsule.id, samples: `${out}/samples.jsonl`, concurrency: 2 }))
} finally { await mf.dispose() }

if (!indexed) {
  const child = spawnSync(process.execPath, [import.meta.filename, site, `${out}/indexed`, 'indexed'], { stdio: 'inherit' })
  if (child.error) throw child.error
  strictEqual(child.status, 0, 'indexed-only capsule parity')
}
