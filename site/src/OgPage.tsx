import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useScans } from './scan'
import { Treemap } from './Treemap'
import { DEFAULT_STORE, storeUrl, type Store } from './stores'
import type { TreeNode } from './types'
import type { UserIndexEntry } from './colors'

// `<store>/og` — a redacted, fixed-size (1200×630) render of that store's
// treemap, used only to screenshot its public og:image. Reuses <Treemap redact>
// so the cell layout + colors match the live view exactly, but every text
// detail is dropped: no cell labels, no $/byte totals, no user names.
const EMPTY_USERS = new Map<string, UserIndexEntry>()
const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']

// The og image colours by top-level prefix (no user names, no ownership), so
// its legend is the same slot assignment <Treemap> derives internally.
const treeLegend = (root: TreeNode): [string, string][] => {
  const bytes = new Map<string, number>()
  for (const bucket of root.c ?? [])
    for (const d of bucket.c ?? []) {
      const k = d.n.startsWith('(') ? '(other)' : d.n
      bytes.set(k, (bytes.get(k) ?? 0) + d.b)
    }
  return [...bytes.entries()]
    .filter(([k]) => k !== '(other)')
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([k], i): [string, string] => [k, `var(${SLOTS[i]})`])
}

export function OgPage({ store = DEFAULT_STORE }: { store?: Store }) {

  useEffect(() => {
    const prev = document.documentElement.dataset.theme
    document.documentElement.dataset.theme = 'dark'
    return () => {
      if (prev) document.documentElement.dataset.theme = prev
      else delete document.documentElement.dataset.theme
    }
  }, [])

  // The latest scan's root view at the og card's pixel budget (1200×630) —
  // the same `/api/subtree` the map draws, through the query cache.
  const asof = useScans(store).data?.[0]
  const treeQ = useQuery<{ tree: TreeNode }>({
    queryKey: ['subtree', store.key, asof, '', 1200, '', 'og'],
    enabled: !!asof,
    staleTime: Infinity,
    queryFn: async () => {
      const r = await fetch(storeUrl(`/api/subtree?date=${asof}&path=&w=1200&h=630`, store), { credentials: 'include' })
      if (!r.ok) throw new Error(`subtree: ${r.status}`)
      return r.json()
    },
  })
  const tree = treeQ.data?.tree ?? null

  const legend = useMemo(() => (tree ? treeLegend(tree) : []), [tree])
  // A store with one bucket opens inside it (as the live map does): the
  // bucket level would otherwise be one full-width box in one hue, while the
  // legend below names the bucket's children.
  const initialPath = useMemo(() => (tree && tree.c?.length === 1 ? [tree, tree.c[0]] : undefined), [tree])

  return (
    <div className="og">
      <div className="og-head">
        <h1>{store.title}</h1>
        <p>{store.desc}</p>
      </div>
      <div className="og-map">
        {tree && (
          <Treemap
            root={tree}
            mode="tree"
            userIdx={EMPTY_USERS}
            dateRange={null}
            scheme={store.scheme}
            initialPath={initialPath}
            redact
          />
        )}
      </div>
      <div className="og-legend">
        {legend.map(([k, c]) => (
          <span className="li" key={k}>
            <span className="sw" style={{ background: c }} />
            {k}
          </span>
        ))}
      </div>
    </div>
  )
}
