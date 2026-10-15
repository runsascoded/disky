#!/usr/bin/env node
// The app icon set from disky's own mark (`scripts/marks/`): the mark inset on Apple's 1024 icon grid
// (an 824 body, 100 margin, a soft drop shadow), rasterized and handed to `cargo tauri icon`, which writes
// `src-tauri/icons/` (icns, ico, pngs; the mobile sets are dropped). The menu-bar `tray-template@2x.png` is drawn by hand and untouched.
// Needs `rsvg-convert` and `cargo tauri` on PATH.
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { inner, mark } from '../../../scripts/marks/marks.mjs'

const key = process.argv[2] ?? 'local'
const body = inner(mark(key))
const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
<defs><filter id="shadow" x="-10%" y="-10%" width="120%" height="125%"><feDropShadow dx="0" dy="10" stdDeviation="14" flood-color="#000" flood-opacity=".3"/></filter></defs>
<g filter="url(#shadow)" transform="translate(100 100) scale(${824 / 512})">${body}</g></svg>`

const dir = mkdtempSync(join(tmpdir(), 'disky-icon-'))
const png = join(dir, 'icon-1024.png')
writeFileSync(join(dir, 'icon.svg'), svg)
execFileSync('rsvg-convert', ['-w', '1024', '-h', '1024', '-o', png, join(dir, 'icon.svg')])
const icons = new URL('../src-tauri/icons', import.meta.url).pathname
execFileSync('cargo', ['tauri', 'icon', png, '-o', icons], { stdio: 'inherit' })
rmSync(dir, { recursive: true })
// disky is macOS-only: drop the mobile sets `cargo tauri icon` also writes.
for (const d of ['android', 'ios']) rmSync(join(icons, d), { recursive: true, force: true })
