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
export async function captureFixture(site: string, output: string): Promise<CapsuleFile> {
  const base = join(site, 'functions/_lib/fixtures/iv')
  const objects: CapsuleFile['objects'] = []
  const walk = async (directory: string): Promise<void> => {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const file = join(directory, entry.name)
      if (entry.isDirectory()) await walk(file)
      else if (/\.(json|parquet)$/.test(entry.name)) objects.push({ key: relative(base, file), file: relative(site, file), sha256: sha256(await readFile(file)) })
    }
  }
  await walk(join(base, 'interval-store/g1'))
  objects.sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0)
  const dates = JSON.parse(await readFile(join(base, 'interval-store/g1/scans.json'), 'utf8')).scans.map((s: { id: string }) => s.id)
  const sql = await readFile(join(site, 'schema.sql'), 'utf8') + LEDGER
  const capsule: Omit<Capsule, 'id'> = {
    version: 1,
    revision: { store: 'synthetic-iv', generation: 'g1', manifest: 'base-only', ledger: 'action-1', deletion: 'none' },
    dates,
    config: { PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', FILTER_INDEXED_ONLY: '0' },
  }
  const id = sha256(JSON.stringify({ capsule, sql, objects }))
  const document = { capsule: { id, ...capsule }, sql, objects }
  await writeFile(output, JSON.stringify(document, null, 2) + '\n')
  return document
}

/** Whole answers are compared; these cover multiple versions, zero-byte files, threshold remainders and scopes. */
export function fixtureRequests(capsule: Capsule): { name: string; request: KernelRequest }[] {
  const base = { version: 1 as const, capsule: capsule.id, budgets: { deadlineMs: 60_000, gets: 10_000, bytes: 32 * 1024 * 1024, outputBytes: 4 * 1024 * 1024 } }
  const options = { path: '', w: 30, h: 30, minArea: 12, atten: 2 }
  const map = (date: string, extra = {}): KernelRequest => ({ ...base, kind: 'map', options: { ...options, date, ...extra } })
  const diff = (extra = {}): KernelRequest => ({ ...base, kind: 'diff', options: { ...options, from: '2026-07-30', to: '2026-08-05', top: 100, ...extra } })
  return [
    { name: 'gets-budget', request: { ...map('2026-08-03'), budgets: { ...base.budgets, gets: 0 } } },
    { name: 'bytes-budget', request: { ...map('2026-08-03'), budgets: { ...base.budgets, bytes: 0 } } },
    ...capsule.dates.map(date => ({ name: `root-${date}`, request: map(date) })),
    { name: 'drill', request: map('2026-08-03', { path: 'b2/e' }) },
    { name: 'remainder', request: map('2026-08-03', { w: 8, h: 6 }) },
    { name: 'owner-ledger', request: map('2026-08-03', { lens: { key: 'alice' } }) },
    { name: 'owner-pool', request: map('2026-08-03', { owner: 'unowned' }) },
    { name: 'class', request: map('2026-08-03', { classes: ['2'] }) },
    { name: 'invalid-query', request: map('2026-08-03', { query: '"' }) },
    { name: 'name', request: map('2026-08-03', { query: '.json' }) },
    { name: 'name-drill', request: map('2026-08-03', { path: 'b2', query: 'e' }) },
    { name: 'diff-root', request: diff() },
    { name: 'diff-drill', request: diff({ path: 'b2/e' }) },
    { name: 'diff-name', request: diff({ query: '.json' }) },
    { name: 'diff-owner-class', request: diff({ owner: 'unowned', classes: ['1'] }) },
    { name: 'diff-cut', request: diff({ top: 1, lookupCap: 0 }) },
    { name: 'unavailable-scan', request: map('1900-01-01') },
    { name: 'wrong-capsule', request: { ...map('2026-08-03'), capsule: 'another-capsule' } },
    { name: 'deadline', request: { ...map('2026-08-03'), budgets: { ...base.budgets, deadlineMs: 0 } } },
    { name: 'output-budget', request: { ...map('2026-08-03'), budgets: { ...base.budgets, outputBytes: 0 } } },
  ]
}
