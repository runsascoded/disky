import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import ts from 'typescript'
import { expect, it } from 'vitest'

it('dedupes and aggregates a large hour within a 96 MiB JS heap', () => {
  const root = resolve(import.meta.dirname, '../../../..')
  mkdirSync(resolve(root, 'tmp'), { recursive: true })
  const dir = mkdtempSync(resolve(root, 'tmp/r2-access-memory-'))
  writeFileSync(resolve(dir, 'package.json'), '{"type":"commonjs"}\n')
  for (const file of ['index.ts', 'event-ids.ts', 'json-stream.ts']) {
    const source = readFileSync(new URL(file, import.meta.url), 'utf8')
    writeFileSync(resolve(dir, file.replace('.ts', '.js')), ts.transpileModule(source, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText)
  }
  for (const file of ['large-hour.cjs', 'worker-streams.cjs']) {
    writeFileSync(resolve(dir, file), readFileSync(new URL(`fixtures/${file}`, import.meta.url)))
  }
  const result = spawnSync(process.execPath, ['--max-old-space-size=96', 'large-hour.cjs'], {
    cwd: dir, encoding: 'utf8', timeout: 30_000, maxBuffer: 1024 * 1024,
  })
  writeFileSync(resolve(dir, 'stderr.txt'), result.stderr ?? '')
  expect({ status: result.status, signal: result.signal, error: result.error?.message }).toEqual({ status: 0, signal: null, error: undefined })
  expect(JSON.parse(result.stdout)).toEqual({ events: 700_000, rows: 140_000, actions: { HeadObject: 280_000, ListObjectsV2: 420_000 } })
}, 35_000)
