// The `/meta` secondary store (STORES_EXTRA): the data behind disk.rbw.sh, i.e. the `disk-tree` R2 bucket (captures,
// path indexes, snapshots, static name indexes), scanned beside each laptop capture by the Batch ingest
// (`infra/aws/ingest.sh` `META_STATIC_GEN`). Admins only: its keys name the capture hosts.
import type { Store } from '../stores'

const store: Store = {
  key: 'meta',
  label: 'Meta',
  title: 'm3 disky metadata',
  desc: 'The data behind disk.rbw.sh: captures, path indexes, snapshots and search indexes in the disk-tree R2 bucket.',
  path: '/meta',
  scheme: 'r2://',
  base: '/data/meta',
  ogImage: '/og.jpg',
  prices: false,
  defaultColor: 'date',
  staging: false,
  owners: false,
  executor: 'laptop',
  scope: 'admin',
  buckets: ['disk-tree'],
  rootLabel: 'm3 disky metadata',
  objectsNote: 'Objects are captures and derived indexes. A meta scan runs after its laptop scan’s outputs land, so it counts them.',
  wall: {
    restrict: 'The meta store is limited to this deployment’s admins.',
    signIn: 'Sign in',
  },
}

export default store
