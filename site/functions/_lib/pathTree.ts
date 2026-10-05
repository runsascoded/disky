/** A list of prefixes as an indented tree, for a Slack code block: shared
 * parents print once, single-child chains collapse into one line
 * (`marin-us-central1/grug/tied_experts/`), and sibling leaves pack onto
 * wrapped lines (`d1024/ d1280/ d512/ d768/`). The scheme is dropped. */

interface Node { kids: Map<string, Node>; leaf: boolean }
interface Line { text: string; leaves: number }

const newNode = (): Node => ({ kids: new Map(), leaf: false })
const byName = (a: [string, Node], b: [string, Node]) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)

function lines(n: Node, indent: string, width: number, out: Line[]): void {
  const kids = [...n.kids.entries()].sort(byName)
  const packed = kids.filter(([, k]) => k.leaf && !k.kids.size)
  const dirs = packed.length > 1 ? kids.filter(([, k]) => !(k.leaf && !k.kids.size)) : kids
  if (packed.length > 1) {
    let words: string[] = []
    const flush = () => { if (words.length) out.push({ text: indent + words.join(' '), leaves: words.length }); words = [] }
    for (const [name] of packed) {
      const w = `${name}/`
      if (words.length && indent.length + [...words, w].join(' ').length > width) flush()
      words.push(w)
    }
    flush()
  }
  for (const [name, k] of dirs) {
    // A single-child chain that isn't itself staged collapses into one line.
    let label = name
    let node = k
    while (!node.leaf && node.kids.size === 1) {
      const [cn, ck] = [...node.kids.entries()][0]
      label += `/${cn}`
      node = ck
    }
    out.push({ text: `${indent}${label}/`, leaves: node.leaf ? 1 : 0 })
    if (node.kids.size) lines(node, indent + '  ', width, out)
  }
}

/** At most `maxLines` lines; what's cut is summarised as `… N more`. */
export function pathTree(prefixes: readonly string[], { maxLines = 24, width = 64 }: { maxLines?: number; width?: number } = {}): string[] {
  const root = newNode()
  for (const p of prefixes) {
    let n = root
    for (const s of p.replace(/^[a-z0-9]+:\/\//, '').replace(/\/$/, '').split('/')) {
      let k = n.kids.get(s)
      if (!k) n.kids.set(s, (k = newNode()))
      n = k
    }
    n.leaf = true
  }
  const all: Line[] = []
  lines(root, '', width, all)
  if (all.length <= maxLines) return all.map(l => l.text)
  const kept = all.slice(0, maxLines - 1)
  const cut = all.slice(maxLines - 1).reduce((t, l) => t + l.leaves, 0)
  return [...kept.map(l => l.text), `… ${cut} more`]
}
