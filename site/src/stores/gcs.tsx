// gcs.oa.dev's store (oa-decoupling step 6): its registry entry, contact and About text.
import type { ExternalLink, Store } from '../stores'

const CONSOLE = 'https://console.cloud.google.com/storage/browser'
const enc = (segs: string[]) => segs.map(encodeURIComponent).join('/')

/** A path's page in the Cloud Console's storage browser (`Store.externalLink`): the bucket, a folder (its prefix,
 *  trailing `/`) or an object's details page. The store root has none. */
export function consoleLink(segs: string[], dir: boolean): ExternalLink | undefined {
  const [bucket, ...rest] = segs
  if (!bucket) return undefined
  const label = 'in the Cloud Console'
  if (!rest.length) return { href: `${CONSOLE}/${enc([bucket])}`, label }
  return dir
    ? { href: `${CONSOLE}/${enc([bucket, ...rest])}/`, label }
    : { href: `${CONSOLE}/_details/${enc([bucket, ...rest])}`, label }
}

const store: Store = {
  key: 'gcs',
  label: 'GCS',
  title: 'Marin GCS usage',
  desc: 'Per-user storage ownership across the six marin-* GCS buckets.',
  path: '/',
  scheme: 'gs://',
  base: '/data',
  ogImage: '/og.jpg',
  prices: true,
  staging: true,
  owners: true,
  executor: 'sweep',
  lifecycle: { tracked: 'job/lifecycle/', rules: 'gcs', grouped: true, recordedFrom: '2026-09-16' },
  buckets: ['marin-us-central2', 'marin-us-central1', 'marin-us-east1', 'marin-us-east5', 'marin-us-west4', 'marin-eu-west4'],
  externalLink: consoleLink,
  rootLabel: 'all buckets',
  objectsNote: 'GCS objects are written by the training jobs and rarely rewritten in place, so created is the object’s upload time; the read axis adds when it was last read.',
  wall: {
    restrict: 'Access is limited to marin contributors and invited collaborators.',
    signIn: 'Sign in',
    how: 'Use Google with any allow-listed account (Stanford, personal, or Open Athena), or have a one-time code emailed to any allow-listed address (no Google account needed). Invited guests can also use a personal share link.',
  },
  contact: {
    email: 'marin-gcs-usage@openathena.ai',
    chat: { label: '#internal-discuss on the Marin Discord', href: 'https://discord.com/channels/1354881461060243556/1412294350645493840' },
  },
  about: (
    <p>
      Storage across the six <code>marin-*</code> GCS buckets — a full per-object listing
      (deduped), snapshotted daily by the{' '}
      <a href="https://github.com/Open-Athena/disky/blob/gcs/AGENTS.md#data-flow" target="_blank" rel="noreferrer"><code>disky</code></a>{' '}
      pipeline, which also ingests the buckets’ access logs and joins ownership onto every prefix
      (W&amp;B run/config matching, executor sidecars, manual curation). Owner assignments apply live on
      top of the latest snapshot. Access logging began 8/13, so “never read” means “not since then”.
    </p>
  ),
}

export default store
