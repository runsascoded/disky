// The public r2.rbw.sh demo's `/meta` secondary store (`STORES_EXTRA` in wrangler.r2.toml): the data behind the
// site — the daily ingest's indexes, snapshots and name index in its own bucket, disk-tree-demo — scanned by the
// same ingest after the primary (`meta-l2/`, `snapshots/meta/`) and served with `store=meta`. Public as the
// primary is; no ownership ledger, no trash gesture, no prices.
import type { Store } from '../stores'

const store: Store = {
  key: 'meta',
  label: 'Meta',
  title: 'Public R2 Treemap Demo disky metadata',
  desc: 'The data behind r2.rbw.sh: the daily scans’ path stores, snapshots, static name index and interval store in disk-tree-demo (R2) — treemap, sizes over time, and diffs.',
  path: '/meta',
  scheme: 'r2://',
  base: '/data/meta',
  ogImage: '/og.jpg',
  prices: false,
  staging: false,
  owners: false,
  executor: 'plan-sweep',
  buckets: ['disk-tree-demo'],
  rootLabel: 'the demo’s own bucket',
  objectsNote: 'Scan outputs are written once per daily run and never rewritten in place, so modified is the object’s publish time. A meta scan lists the bucket before writing its own outputs.',
  wall: { restrict: 'This store is public.', signIn: 'Sign in' },
}

export default store
