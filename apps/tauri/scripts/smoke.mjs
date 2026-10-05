#!/usr/bin/env node
// Exercise the extracted bundle with a new home/config and only system tools on PATH.
import assert from 'node:assert/strict'
import { execFileSync, spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { join, resolve } from 'node:path'

const app = resolve(process.argv[2] ?? 'apps/tauri/target/release/bundle/macos/disky.app')
const scratch = resolve(process.argv[3] ?? 'tmp/app-smoke')
mkdirSync(scratch, { recursive: true })
const home = mkdtempSync(join(scratch, 'home-'))
const cfg = join(home, '.config/disk-tree')
const fixture = join(home, 'fixture')
mkdirSync(cfg, { recursive: true })
mkdirSync(fixture)
writeFileSync(join(fixture, 'one.txt'), 'one\n')
writeFileSync(join(fixture, 'two.txt'), 'two\n')
const exe = join(app, 'Contents/MacOS/disky')
// A real user's launchd domain is shared even with a different HOME. Simulate
// an unregistered agent so this test can never kickstart an installed scan.
const shim = join(home, 'test-tools')
mkdirSync(shim)
writeFileSync(join(shim, 'launchctl'), '#!/bin/sh\nexit 1\n', { mode: 0o755 })
const env = { HOME: home, PATH: `${shim}:/usr/bin:/bin:/usr/sbin:/sbin`, DISK_TREE_ROOT: cfg }
const run = (...args) => execFileSync(exe, args, { env, encoding: 'utf8', timeout: 60000 })
const defaults = { site: 'local', scope: 'machine', schedule: ['06:00', '18:00'] }
assert.deepEqual(JSON.parse(run('settings', 'show')), defaults)
assert.match(run('version'), /^disky \d+\.\d+\.\d+(?:-[\w.]+)?\n$/)
writeFileSync(join(cfg, 'disky.json'), JSON.stringify({ settings: { scope: 'home' } }) + '\n')
run('scan', 'now')
for (let n = 0; n < 600; n++) {
  const statePath = join(cfg, 'disky-state.json')
  if (existsSync(statePath)) {
    const state = JSON.parse(readFileSync(statePath)).scan
    if (state.last_end != null && state.pid == null) {
      assert.equal(state.last_exit, 0)
      assert.equal(state.force, false)
      break
    }
  }
  if (n === 599) throw new Error('On-demand scan did not finish')
  await new Promise(resolve => setTimeout(resolve, 100))
}
assert.equal(existsSync(join(home, 'Library/LaunchAgents')), false)
const scans = join(home, 'Library/Application Support/disky/scans')
const ids = readdirSync(scans)
assert.equal(ids.length, 1)
const meta = JSON.parse(readFileSync(join(scans, ids[0], 'meta.json')))
assert.ok(meta.total_objects > 0)
const expectedBytes = ['one.txt', 'two.txt'].reduce((sum, name) => sum + statSync(join(fixture, name)).blocks * 512, 0)

// Reserve an available loopback port. Explicit occupied ports must not be reused.
const probe = createServer()
await new Promise((resolve, reject) => { probe.once('error', reject); probe.listen(0, '127.0.0.1', resolve) })
const port = probe.address().port
await new Promise(resolve => probe.close(resolve))
const server = spawn(exe, ['serve', '--addr', `127.0.0.1:${port}`], { env, stdio: ['ignore', 'pipe', 'pipe'] })
let stderr = ''
server.stderr.on('data', data => { stderr += data })
try {
  let response
  for (let n = 0; n < 100; n++) {
    if (server.exitCode != null) throw new Error(`Server exited ${server.exitCode}: ${stderr}`)
    try { response = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(1000) }); break } catch (error) {
      if (error.cause?.code !== 'ECONNREFUSED') throw error
      await new Promise(resolve => setTimeout(resolve, 100))
    }
  }
  assert.equal(response?.status, 200, stderr)
  const html = await response.text()
  const page = JSON.parse(html.match(/window\.__DISKY__ = (.+?)<\/script>/)[1])
  assert.deepEqual({ home: page.home, staging: page.staging }, { home: home.slice(1), staging: false })
  assert.match(page.title, /^disky — .+$/)
  const p = new URLSearchParams({ date: ids[0], path: fixture.slice(1), depth: '1' })
  const tree = await fetch(`http://127.0.0.1:${port}/api/subtree?${p}`).then(r => { assert.equal(r.status, 200); return r.json() })
  assert.deepEqual({ bytes: tree.tree.b, objects: tree.tree.o }, { bytes: expectedBytes, objects: 2 })
  assert.deepEqual(tree.tree.c.map(n => n.n).sort(), ['one.txt', 'two.txt'])
  const list = await fetch(`http://127.0.0.1:${port}/data/laptop/scans.json`).then(r => r.json())
  assert.deepEqual(list, ids)
  const outside = await fetch(`http://127.0.0.1:${port}/api/plans`)
  assert.equal(outside.status, 404)
  console.log(JSON.stringify({ status: 'passed', app, home, scan: ids[0], files: 2, bytes: expectedBytes }, null, 2))
} finally {
  server.kill('SIGTERM')
  await new Promise(resolve => { if (server.exitCode != null || server.signalCode != null) resolve(); else server.once('exit', resolve) })
}
