import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { regex, simple, SYNTAXES } from '../functions/_lib/querySyntax'
import { QueryHelpCard } from './QueryHelp'
import { INDEXED_HELP } from '../functions/_lib/indexedOnly'

const render = (active = simple, onPick?: (id: string) => void) =>
  renderToStaticMarkup(createElement(QueryHelpCard, { syntaxes: SYNTAXES, active, onPick }))

/** The card's form rows as `[form, meaning, example]` (markup unescaped). */
const rows = (html: string): string[][] =>
  [...html.matchAll(/<tr>(.*?)<\/tr>/g)].map(m =>
    [...m[1].matchAll(/<td>(.*?)<\/td>/g)].map(c => c[1].replace(/<\/?code>/g, '').replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&amp;/g, '&')))

describe('the filter box’s help card: generated from `describe()`', () => {
  it('`simple`: one row per form, `-x` excludes like GitHub search', () => {
    expect(rows(render())).toEqual([
      ['text', 'substring anywhere in the path', 'ckpt'],
      ['a b', 'both (AND), in any segments', 'ckpt final'],
      ['a|b', 'either (OR; looser than AND)', 'ttl=7d|tmp'],
      ['-x', 'exclude, like GitHub search: drops what contains x and subtracts its bytes', 'ckpt -tmp'],
      ['*', 'any characters within one name', '*.safetensors'],
      ['^x', 'a file or folder name starting with x', '^ckpt'],
      ['x$', 'a file or folder name ending with x', '.safetensors$'],
      ['^x$', 'a file or folder named exactly x', '^config.json$'],
      ['"…"', 'literal: spaces, a leading -, |, *, ^, $', '"a b"'],
      ['a/b', 'a term with / spans segments', 'run-a/ckpt'],
    ])
  })
  it('the filter box’s and the indexed-only help each end on the one line about literal `^`/`$`', () => {
    const line = '^/$ anchor a name; write \\^ or \\$ (or quote it) to search them literally.'
    expect([simple.describe().notes.at(-1), INDEXED_HELP.notes.at(-1)]).toEqual([line, line])
  })
  it('any syntax renders its own forms', () => {
    for (const s of SYNTAXES) expect([s.id, rows(render(s))]).toEqual([s.id, s.describe().forms.map(f => [f.form, f.meaning, f.example])])
  })
  it('`regex`, read-only picker: the whole card', () => {
    expect(render(regex)).toBe(
      '<div class="qhelp-card"><div class="qhelp-head"><span>Filter syntax:</span><b>regex</b></div>' +
      '<p>one JavaScript regex over the full path (bucket/dir/…), case-insensitive</p>' +
      '<table><tbody>' +
      '<tr><td><code>re</code></td><td>matches anywhere in the path</td><td><code>ckpt.*final</code></td></tr>' +
      '<tr><td><code>^…</code></td><td>anchored at the bucket (the full path’s start, not a name’s)</td><td><code>^my-bucket/tmp/</code></td></tr>' +
      '<tr><td><code>…$</code></td><td>anchored at the name’s end</td><td><code>\\.safetensors$</code></td></tr>' +
      '<tr><td><code>[^/]*</code></td><td>stay within one name</td><td><code>ckpt[^/]*final</code></td></tr>' +
      '</tbody></table>' +
      '<p class="qhelp-note">No exclusion; the search index never serves a regex, so big views read slower.</p></div>',
    )
  })
  it('with `onPick`: a button per registered syntax, the active one pressed', () => {
    const head = /<div class="qhelp-head">(.*?)<\/div>/.exec(render(simple, () => {}))![1]
    expect(head).toBe('<span>Filter syntax:</span><button type="button" class="on" aria-pressed="true">simple</button><button type="button" class="" aria-pressed="false">regex</button>')
  })
})
