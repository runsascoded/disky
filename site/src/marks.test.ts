import { describe, expect, it } from 'vitest'
import { markLinks } from './marks'
import type { Store } from './stores'

const MODULES = Object.entries(import.meta.glob<{ default: Store }>('./stores/*.{ts,tsx}', { eager: true }))

describe('store marks', () => {
  it("a mark's links are the ones the build writes for `MARK`", () => {
    expect(markLinks('nas')).toEqual([
      { rel: 'icon', type: 'image/svg+xml', href: '/icons/nas.svg' },
      { rel: 'icon', sizes: '32x32', href: '/icons/nas.ico' },
      { rel: 'apple-touch-icon', href: '/icons/nas-180.png' },
    ])
  })

  it("every store's own mark is built into public/icons, as an unknown `MARK` fails the build", async () => {
    const { existsSync } = (await import(/* @vite-ignore */ 'node:fs' as string)) as { existsSync(p: string | URL): boolean }
    const marked = MODULES.filter(([, m]) => m.default.mark).map(([file, m]) => [file, m.default.mark] as const)
    const missing = marked.flatMap(([file, mark]) =>
      markLinks(mark!).map(l => `public${l.href}`).filter(p => !existsSync(new URL(`../${p}`, import.meta.url))).map(p => `${file}: ${p}`))
    expect(missing).toEqual([])
  })
})
