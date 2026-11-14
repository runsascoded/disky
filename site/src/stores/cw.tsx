// cw-s3.oa.dev's store (oa-decoupling step 6).
import type { Store } from '../stores'

const store: Store = {
  key: 'cw',
  label: 'CoreWeave',
  title: 'Marin CoreWeave usage',
  desc: 'Storage usage of the Marin CoreWeave buckets — treemap, diffs over time, and reviewed deletions.',
  about: (
    <p>
      Every object in Marin’s CoreWeave buckets (<code>marin-us-east-02a</code>, <code>hero-checkpoints</code>, <code>marin-us-east-06a</code>, <code>rhoarnet-us-east-08a</code>, <code>marin-us-east-08a</code>, <code>marin-us-west-04a</code>, <code>marin-us-east-01a</code>, <code>marin-us-east-04a</code>, <code>marinfold-exp368-rno2a</code>, <code>marin-rno2a</code>),
      listed every 6 hours and summed by directory. Sizes are bytes stored; “created” is each object’s upload time.
    </p>
  ),
  path: '/',
  scheme: 's3://',
  base: '/data/cw',
  ogImage: '/og.jpg',
  prices: false,
  staging: true,   // the table's trash gesture + /staged (needs `stage_batches`, migrations/cw/0002)
  owners: false,
  executor: 'plan-sweep',
  lifecycle: { tracked: 'job/cw-lifecycle.json', rules: 's3', recordedFrom: '2026-09-17' },
  buckets: ['marin-us-east-02a', 'hero-checkpoints', 'marin-us-east-06a', 'rhoarnet-us-east-08a', 'marin-us-east-08a', 'marin-us-west-04a', 'marin-us-east-01a', 'marin-us-east-04a', 'marinfold-exp368-rno2a', 'marin-rno2a'],
  // CoreWeave's own per-bucket totals and zone quotas, sampled more often than the 6-hourly scans.
  liveTotals: { label: 'Live bucket totals (Grafana)', href: 'https://grafana.oa.dev/d/marin-storage/storage?orgId=1&from=now-7d&to=now&timezone=utc' },
  rootLabel: 'all buckets',
  objectsNote: 'CoreWeave objects are written once by the training jobs and never rewritten in place, so created is the object’s only time.',
  wall: {
    restrict: 'Access is limited to Open Athena and CoreWeave staff.',
    signIn: 'Sign in with Open Athena',
    how: 'Use Google with your Open Athena or CoreWeave account, or have a one-time code emailed to it (no Google account needed). Invited guests can also use a personal share link.',
  },
}

export default store
