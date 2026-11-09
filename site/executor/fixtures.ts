/** Capture the existing synthetic interval-store bytes; never reads a deployment or its credentials. */
import { readFile, readdir, writeFile } from 'node:fs/promises'
import { join, relative } from 'node:path'
import { sha256, type CapsuleFile } from './node.js'
import type { Capsule, KernelRequest } from './kernel.js'

const LEDGER = `
CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL);
CREATE TABLE owner_prefixes (prefix TEXT NOT NULL, kind TEXT, owner TEXT, ts INTEGER NOT NULL, action_id INTEGER NOT NULL, tombstoned TEXT);
INSERT INTO actions VALUES (1, 'reviewer@example.test');
INSERT INTO owner_prefixes VALUES ('gs://b1/d/', 'prefix', 'alice', 1, 1, NULL);
`
export async function captureFixture(site: string, output: string, generation = 'g1'): Promise<CapsuleFile> {
  const base = join(site, 'functions/_lib/fixtures/iv')
  const objects: CapsuleFile['objects'] = []
  const walk = async (directory: string): Promise<void> => {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = join(directory, entry.name)
      if (entry.isDirectory()) await walk(file)
      else if (/\.(json|parquet)$/.test(entry.name)) objects.push({ key: relative(base, file), file: relative(site, file), sha256: sha256(await readFile(file)) })
    }
  }
  await walk(join(base, `interval-store/${generation}`))
  objects.sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0)
  const dates = JSON.parse(await readFile(join(base, `interval-store/${generation}/scans.json`), 'utf8')).scans.map((s: { id: string }) => s.id)
  const sql = await readFile(join(site, 'schema.sql'), 'utf8') + (generation === 'g2' ? LEDGER.replace('gs://b1/d/', 'gs://bk/nest/') : LEDGER)
  const capsule: Omit<Capsule, 'id'> = {
    version: 1,
    revision: { store: 'synthetic-iv', generation, manifest: 'base-only', ledger: 'action-1', deletion: 'none' },
    dates,
    config: { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: generation, FILTER_INDEXED_ONLY: '0' },
  }
  const id = sha256(JSON.stringify({ capsule, sql, objects }))
  const document = { capsule: { id, ...capsule }, sql, objects }
  await writeFile(output, JSON.stringify(document, null, 2) + '\n')
  return document
}

/** Whole answers are compared; these cover multiple versions, zero-byte files, threshold remainders and scopes. */
export interface FixtureCase { name: string; request: KernelRequest; cancelled?: boolean }
export function fixtureRequests(capsule: Capsule): FixtureCase[] {
  const base = { version: 1 as const, capsule: capsule.id, budgets: { deadlineMs: 60_000, gets: 10_000, bytes: 32 * 1024 * 1024, outputBytes: 4 * 1024 * 1024 } }
  const options = { path: '', w: 30, h: 30, minArea: 12, atten: 2 }
  const map = (date: string, extra = {}): KernelRequest => ({ ...base, kind: 'map', options: { ...options, date, ...extra } })
  const diff = (extra = {}): KernelRequest => ({ ...base, kind: 'diff', options: { ...options, from: '2026-07-30', to: '2026-08-05', top: 100, ...extra } })
  return [
    { name: 'cancelled', request: map('2026-08-03'), cancelled: true },
    { name: 'gets-budget', request: { ...map('2026-08-03'), budgets: { ...base.budgets, gets: 0 } } },
    { name: 'bytes-budget', request: { ...map('2026-08-03'), budgets: { ...base.budgets, bytes: 0 } } },
    { name: 'invalid-budgets', request: { ...map('2026-08-03'), budgets: JSON.parse('{"gets":1,"bytes":1,"deadlineMs":100,"junk":1}') } },
    ...capsule.dates.map(date => ({ name: `root-${date}`, request: map(date) })),
    { name: 'drill', request: map('2026-08-03', { path: 'b2/e' }) },
    { name: 'remainder', request: map('2026-08-03', { w: 8, h: 6 }) },
    { name: 'diff-root', request: diff() },
    { name: 'diff-drill', request: diff({ path: 'b2/e' }) },
    { name: 'diff-cut', request: diff({ top: 1, lookupCap: 0 }) },
    { name: 'unavailable-scan', request: map('1900-01-01') },
    { name: 'wrong-capsule', request: { ...map('2026-08-03'), capsule: 'another-capsule' } },
    { name: 'deadline', request: { ...map('2026-08-03'), budgets: { ...base.budgets, deadlineMs: 0 } } },
    { name: 'output-budget', request: { ...map('2026-08-03'), budgets: { ...base.budgets, outputBytes: 0 } } },
  ]
}


export function sliceRequests(capsule: Capsule): { name: string; request: KernelRequest }[] {
  const base = { version: 1 as const, capsule: capsule.id, budgets: { deadlineMs: 60_000, gets: 10_000, bytes: 32 * 1024 * 1024, outputBytes: 4 * 1024 * 1024 } }
  const options = { path: '', w: 1280, h: 800, minArea: 12, atten: 1 }
  const scopes = [
    { name: 'all', options: {} },
    { name: 'alice-ledger', options: { lens: { key: 'alice' } } },
    { name: 'bob', options: { lens: { key: 'bob' } } },
    { name: 'owned', options: { owner: 'owned' as const } },
    { name: 'unowned', options: { owner: 'unowned' as const } },
    { name: 'except-alice', options: { owner: { not: ['alice'] } } },
    { name: 'class', options: { classes: ['1' as const] } },
    { name: 'owner-class', options: { owner: 'unowned' as const, classes: ['1' as const] } },
  ]
  const reads: { name: string; request: KernelRequest }[] = scopes.flatMap(scope => [
    { name: `map-${scope.name}`, request: { ...base, kind: 'map' as const, options: { ...options, ...scope.options, date: capsule.dates[1] } } },
    { name: `drill-${scope.name}`, request: { ...base, kind: 'map' as const, options: { ...options, ...scope.options, path: 'bk/nest', date: capsule.dates[1] } } },
    { name: `diff-${scope.name}`, request: { ...base, kind: 'diff' as const, options: { ...options, ...scope.options, from: capsule.dates[0], to: capsule.dates[1], top: 100 } } },
  ])
  reads.push(
    { name: 'name', request: { ...base, kind: 'map', options: { ...options, date: capsule.dates[1], query: 'nest' } } },
    { name: 'name-drill', request: { ...base, kind: 'map', options: { ...options, date: capsule.dates[1], path: 'bk', query: 'nest' } } },
    { name: 'diff-name', request: { ...base, kind: 'diff', options: { ...options, from: capsule.dates[0], to: capsule.dates[1], top: 100, query: 'nest' } } },
  )
  return reads
}
