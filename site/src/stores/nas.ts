// A NAS beside a laptop deployment (m3): its array (`capture -h nas /mnt/user`,
// a walk over ssh), ingested under `snapshots/nas/` by the same Batch job
// (`HOST_STORES`), mounted at `/nas` as a secondary store (`STORES_EXTRA`).
import type { Store } from '../stores'

const store: Store = {
  key: 'nas',
  label: 'NAS',
  title: 'disky — nas',
  desc: 'Disk usage of a NAS array (Unraid: XFS data disks + a btrfs cache), captured over ssh.',
  path: '/nas',
  mark: 'nas',
  scheme: 'file:///',
  base: '/data/nas',
  // The capture's root (`/mnt/user`): its paths keep the full path, and nothing else is above it.
  root: ['mnt', 'user'],
  ogImage: '/og.jpg',
  prices: false,
  // Deletes run on the laptop's drainer, which can't reach the NAS's disks.
  staging: false,
  owners: false,
  executor: 'laptop',
  buckets: [],
  rootLabel: 'the array',
  objectsNote: 'Files on the NAS array; created is each file’s mtime at capture time.',
  wall: {
    restrict: 'This NAS’s disk usage is private to its owner.',
    signIn: 'Sign in',
  },
}

export default store
