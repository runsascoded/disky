// The public r2.rbw.sh demo's store: `cloud`'s own deployment
// (wrangler.r2.toml). A deployment branch adds its own entry beside it.
import type { Store } from '../stores'

const store: Store = {
  key: 'r2',
  label: 'R2',
  title: 'disk-tree — public R2 buckets',
  desc: 'Storage usage of public Cloudflare R2 buckets (ctbk, crashes, jc-taxes) — treemap, sizes over time, and diffs. A public disk-tree demo.',
  path: '/',
  scheme: 'r2://',
  base: '/data/r2',
  ogImage: '/og.jpg',
  prices: false,
  staging: false,
  owners: false,
  executor: 'plan-sweep',
  buckets: ['ctbk', 'crashes', 'jc-taxes'],
  // Each bucket's public custom domain (CORS `*`). `jc-taxes` has one too,
  // `data.jct.rbw.sh`, but its CORS admits only jct.rbw.sh (jc-taxes
  // `infra/__main__.py`), so a browser here can't read it: its objects show
  // size and dates until that rule lists this site.
  objectBases: { ctbk: 'https://data.ctbk.dev', crashes: 'https://crashes-data.hccs.dev' },
  rootLabel: 'all buckets',
  objectsNote: 'Public R2 buckets scanned daily by disk-tree; created is each object’s upload time.',
  // A public deploy never shows the wall (`AUTH_MODE === 'public'`); copy
  // kept for the type, and for a gated build of the same store.
  wall: { restrict: 'This store is public.', signIn: 'Sign in' },
}

export default store
