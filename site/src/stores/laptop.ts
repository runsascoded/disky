// A laptop deployment's store (`local`): its captures, served from R2 under
// `/data/laptop`. A laptop deployment branch (m3) builds with `STORE = "laptop"`.
import type { Store } from '../stores'

const store: Store = {
  key: 'laptop',
  label: 'Laptop',
  title: `disk-tree — ${import.meta.env.VITE_ROOT_LABEL || 'laptop'}`,  // `ROOT_LABEL` names the deployment
  desc: 'Disk usage of one laptop, captured every 12 h — treemap, sizes over time, diffs, and staged deletes executed on the laptop.',
  path: '/',
  scheme: 'file:///',  // + the root sans leading slash (dt-cloud path-index strips it)
  home: (import.meta.env.VITE_HOME ?? '').split('/').filter(Boolean),
  base: '/data/laptop',
  ogImage: '/og.jpg',
  prices: false,
  staging: true,
  owners: false,
  // The laptop's own drainer executes runs (`_lib/laptopDispatch.ts`).
  executor: 'laptop',
  // Roots are scanned directories (`/Users/ryan`), not buckets.
  buckets: [],
  rootLabel: 'all roots',
  objectsNote: 'Local files; created is each file’s mtime at capture time.',
  wall: {
    restrict: 'This laptop’s disk usage is private to its owner.',
    signIn: 'Sign in',
  },
}

export default store
