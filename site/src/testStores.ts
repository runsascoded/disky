// Example stores for tests: the shapes the deployments run (a plain-S3 store, a
// GCS store with owners and staging, a secondary `/meta` store), with neutral
// names. `cloud`'s registry holds only r2; tests pass `TEST_REGISTRY` to
// `resolveStores`.
import type { Store } from './stores'
import laptop from './stores/laptop'
import r2 from './stores/r2'

const base = { ogImage: '/og.jpg', querySyntax: undefined, wall: { restrict: 'Members only.', signIn: 'Sign in' } }

export const S3_EXAMPLE: Store = {
  ...base, key: 'cw', label: 'S3', title: 'S3 usage', desc: 'An S3 deployment.', path: '/', scheme: 's3://', base: '/data/cw',
  prices: false, staging: false, owners: false, executor: 'plan-sweep', buckets: ['data-a', 'data-b'], rootLabel: 'all buckets',
  objectsNote: 'Written once.',
}
export const GCS_EXAMPLE: Store = {
  ...base, key: 'gcs', label: 'GCS', title: 'GCS usage', desc: 'A GCS deployment with owners.', path: '/', scheme: 'gs://', base: '/data',
  prices: true, staging: true, owners: true, executor: 'sweep', buckets: ['bucket-1', 'bucket-2', 'bucket-3'], rootLabel: 'all buckets',
  objectsNote: 'Upload time.',
}
export const META_EXAMPLE: Store = {
  ...base, key: 'meta', label: 'Meta', title: 'Our storage — scan & index data', desc: 'The deployments’ own data.', path: '/meta', scheme: 'gs://', base: '/data/meta',
  prices: false, staging: false, owners: false, executor: 'plan-sweep', buckets: ['my-data', 'my-index'], rootLabel: 'our storage',
  objectsNote: 'Publish time.',
}
export const TEST_REGISTRY: Store[] = [S3_EXAMPLE, GCS_EXAMPLE, META_EXAMPLE, laptop, r2]
