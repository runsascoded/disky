// `/api/digest-reapply` (specs/digest-runway-marks.md §4): what a request may name, the digest CLI arguments it
// becomes, and the Batch task's script.
import { describe, expect, it } from 'vitest'
import { batchConfig, type BatchEnv } from '../_lib/batchConfig'
import { PLAN_SWEEP_NEEDS } from '../_lib/cwBatch'
import { parseReapply, reapplyArgs, reapplyConfig, type ReapplyConfig, reapplyScript, reapplySpec, REAPPLY_NEEDS } from './digest-reapply'

const base = { month: '2026-11', target: 'op', dry_run: true }
const CONFIG: ReapplyConfig = { template: 'quota', config: 'job/digest.yml', root: 'snapshots/demo', env_script: 'job/demo-env.sh', r2_bucket: 'demo-index', secrets: { SITE_TOKEN: 'demo-grant' } }

describe('/api/digest-reapply', () => {
  it('reads a target of the month, a channel (default the deployment’s) and candidate settings', () => {
    expect([
      parseReapply(base, 'C0C1YR7D0KU'),
      parseReapply({ ...base, channel: 'D0CE8QE83EH', target: '2026-11-01..2026-11-02', dry_run: false, params: 'qh,pp', quota_marks: 'runway-hue' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: 'thread' }, 'C0C1YR7D0KU'),
    ]).toEqual([
      { month: '2026-11', channel: 'C0C1YR7D0KU', target: 'op', dry_run: true, params: null, quota_marks: null },
      { month: '2026-11', channel: 'D0CE8QE83EH', target: '2026-11-01..2026-11-02', dry_run: false, params: 'qh,pp', quota_marks: 'runway-hue' },
      { month: '2026-11', channel: 'C0C1YR7D0KU', target: 'thread', dry_run: true, params: null, quota_marks: null },
    ])
    const T = 'target: thread | op | a day of 2026-11 | an ordered range of its days (A..B)'
    expect([
      parseReapply(base, undefined),
      parseReapply({ ...base, channel: 'general' }, undefined),
      parseReapply({ ...base, month: '2026-13' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-10-31' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-11-31' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-11-03..2026-11-02' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: '2026-11-01..2026-11-02..2026-11-03' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, target: "op'; rm -rf /" }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, dry_run: 'yes' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, params: 'qx' }, 'C0C1YR7D0KU'),
      parseReapply({ ...base, quota_marks: 'squares' }, 'C0C1YR7D0KU'),
    ]).toEqual([
      'channel: a Slack channel id (no DIGEST_CHANNEL default here)',
      'channel: a Slack channel id (no DIGEST_CHANNEL default here)',
      'month: YYYY-MM', T, T, T, T, T,
      'dry_run: a boolean',
      'params: digest params: unreadable field "qx" in "qx"',
      'quota_marks: warning | circles | runway | runway-hue | arrow-chip',
    ])
  })

  it('takes its deployment’s names from `DIGEST_REAPPLY`, or says what’s missing', () => {
    expect([
      reapplyConfig(JSON.stringify(CONFIG)),
      reapplyConfig(undefined),
      reapplyConfig('nope'),
      reapplyConfig(JSON.stringify({ ...CONFIG, template: 'cw' })),
      reapplyConfig(JSON.stringify({ ...CONFIG, template: 'gcs' })),
      reapplyConfig(JSON.stringify({ ...CONFIG, template: 'a b' })),
      reapplyConfig(JSON.stringify({ ...CONFIG, template: 'demo' })),
      reapplyConfig(JSON.stringify({ ...CONFIG, env_script: "x'; rm -rf /" })),
      reapplyConfig(JSON.stringify({ ...CONFIG, secrets: { lower: 'x' } })),
      reapplyConfig(JSON.stringify({ ...CONFIG, secrets: undefined })),
    ]).toEqual([CONFIG, 'DIGEST_REAPPLY', 'DIGEST_REAPPLY: not JSON', CONFIG, { ...CONFIG, template: 'cost' }, 'DIGEST_REAPPLY.template', 'DIGEST_REAPPLY.template', 'DIGEST_REAPPLY.env_script', 'DIGEST_REAPPLY.secrets', 'DIGEST_REAPPLY.secrets'])
  })

  it('becomes the digest CLI’s selectors; a dry run of the OP or of days also writes its rewrite plan', () => {
    const PLAN = '/gcs/data/digest/reapply/demo-digest-20261103-120000z.plan.json'
    const r = (o: object) => reapplyArgs({ month: '2026-11', channel: 'C1', target: 'op', dry_run: false, params: null, quota_marks: null, ...o }, PLAN)
    expect([
      r({}),
      r({ dry_run: true }),
      r({ target: '2026-11-02', dry_run: true }),
      r({ target: '2026-11-01..2026-11-02', params: '', quota_marks: 'runway-hue' }),
      r({ target: 'thread', dry_run: true, params: 'qh,pp' }),
      reapplyArgs({ month: '2026-11', channel: 'C1', target: 'op', dry_run: true, params: null, quota_marks: null }),
    ]).toEqual([
      ['-m', '2026-11', '-c', 'C1', '-o'],
      ['-m', '2026-11', '-c', 'C1', '-o', '-n', '-j', PLAN],
      ['-m', '2026-11', '-c', 'C1', '-y', '2026-11-02..2026-11-02', '-n', '-j', PLAN],
      ['-m', '2026-11', '-c', 'C1', '-y', '2026-11-01..2026-11-02', '-p', '', '-q', 'runway-hue'],
      ['-m', '2026-11', '-c', 'C1', '-n', '-p', 'qh,pp'],
      ['-m', '2026-11', '-c', 'C1', '-o', '-n'],
    ])
  })

  it('runs the CLI in the scan steps’ environment, its output teed to the run’s file', () => {
    expect(reapplyScript(['-m', '2026-11', '-c', 'C1', '-o', '-p', ''], '/gcs/data/digest/reapply/demo-digest-20261103-120000z.txt', CONFIG).split('\n')).toEqual([
      'set -euo pipefail',
      'cd "${APP_DIR:-/app}"',
      ". 'job/demo-env.sh'",
      '{ set +x; } 2>/dev/null',
      "OUT='/gcs/data/digest/reapply/demo-digest-20261103-120000z.txt'",
      'mkdir -p "$(dirname "$OUT")"',
      `dt-cloud digest -T 'quota' -C 'job/digest.yml' -r "gs://$DATA_BUCKET/"'snapshots/demo' '-m' '2026-11' '-c' 'C1' '-o' '-p' '' 2>&1 | tee "$OUT"`,
    ])
  })

  it('reads a root URL as given: the digest state on R2, the run’s file still on the data bucket', () => {
    const r2: ReapplyConfig = { ...CONFIG, root: 'r2://demo-index/snapshots/demo' }
    expect(reapplyConfig(JSON.stringify(r2))).toEqual(r2)
    expect(reapplyScript(['-m', '2026-11', '-c', 'C1', '-o', '-n'], '/gcs/data/digest/reapply/demo-digest-20261103-120000z.txt', r2).split('\n')).toEqual([
      'set -euo pipefail',
      'cd "${APP_DIR:-/app}"',
      ". 'job/demo-env.sh'",
      '{ set +x; } 2>/dev/null',
      "OUT='/gcs/data/digest/reapply/demo-digest-20261103-120000z.txt'",
      'mkdir -p "$(dirname "$OUT")"',
      `dt-cloud digest -T 'quota' -C 'job/digest.yml' -r 'r2://demo-index/snapshots/demo' '-m' '2026-11' '-c' 'C1' '-o' '-n' 2>&1 | tee "$OUT"`,
    ])
  })

  it('takes only gs://, r2:// and s3:// as a root URL', () => {
    expect(['https://x/snapshots', 'file:///snapshots', 'r2://demo-index/a b', "r2://demo-index/'x"].map(root => reapplyConfig(JSON.stringify({ ...CONFIG, root })))).toEqual([
      'DIGEST_REAPPLY.root', 'DIGEST_REAPPLY.root', 'DIGEST_REAPPLY.root', 'DIGEST_REAPPLY.root',
    ])
    expect(['gs://b/s', 's3://b/s'].map(root => (reapplyConfig(JSON.stringify({ ...CONFIG, root })) as ReapplyConfig).root)).toEqual(['gs://b/s', 's3://b/s'])
  })

  it('takes the job’s image as a tag or digest reference', () => {
    const img = 'us-west1-docker.pkg.dev/p/repo/scan'
    expect([`${img}:54b2ba0914c2`, `${img}@sha256:${'f'.repeat(64)}`, 'scan:latest', `${img}:a b`, `${img}`].map(image => {
      const c = reapplyConfig(JSON.stringify({ ...CONFIG, image }))
      return typeof c === 'string' ? c : c.image
    })).toEqual([`${img}:54b2ba0914c2`, `${img}@sha256:${'f'.repeat(64)}`, 'DIGEST_REAPPLY.image', 'DIGEST_REAPPLY.image', 'DIGEST_REAPPLY.image'])
  })

  it('enabling it never configures plan-sweep dispatch', () => {
    // everything a deployment sets for re-apply alone (r2's shape): none of the sweep executor's image or S3 endpoint
    const env: BatchEnv = { GCP_PROJECT: 'p', BATCH_REGION: 'us-west1', DATA_BUCKET: 'data', CF_ACCOUNT_ID: 'acct', DISKY_LABELS: 'app=disky,deployment=r2' }
    expect('missing' in batchConfig(env, REAPPLY_NEEDS)).toBe(false)
    expect(batchConfig(env, PLAN_SWEEP_NEEDS)).toEqual({ missing: 'SWEEP_IMAGE' })
    expect(PLAN_SWEEP_NEEDS.filter(k => !(REAPPLY_NEEDS as readonly string[]).includes(k))).toEqual(['SWEEP_IMAGE', 'SWEEP_S3_ENDPOINT'])
  })

  it('runs as exactly its own config: its image and secrets, no sweep env or source keys', () => {
    const cfg = batchConfig({ GCP_PROJECT: 'p', BATCH_REGION: 'us-west1', DATA_BUCKET: 'data', CF_ACCOUNT_ID: 'acct', DISKY_LABELS: 'app=disky,deployment=r2' }, REAPPLY_NEEDS)
    if ('missing' in cfg) throw new Error(cfg.missing)
    const c: ReapplyConfig = { ...CONFIG, root: 'r2://demo-index/snapshots/demo', image: 'reg/scan:abc', secrets: { SLACK_BOT_TOKEN: 'demo-bot', R2_ACCESS_KEY_ID: 'demo-r2-id' } }
    expect(JSON.stringify(reapplySpec(cfg, 'job@p', 'reg/scan:abc', 'run', 'quota-digest-20261122-120000z', 'https://demo', c))).toBe(JSON.stringify({
      taskGroups: [{
        taskCount: 1,
        taskSpec: {
          runnables: [{ container: { imageUri: 'reg/scan:abc', entrypoint: '/bin/bash', commands: ['-c', 'run'], volumes: ['/mnt/disks/gcs/data:/gcs/data:rw'] } }],
          computeResource: { cpuMilli: 8000, memoryMib: 16000 },
          maxRetryCount: 0,
          maxRunDuration: '86400s',
          volumes: [{ gcs: { remotePath: 'data' }, mountPath: '/mnt/disks/gcs/data', mountOptions: ['--implicit-dirs'] }],
          environment: {
            variables: { DATA_BUCKET: 'data', JOB_ID: 'quota-digest-20261122-120000z', SITE_URL: 'https://demo', R2_BUCKET: 'demo-index', CLOUDFLARE_ACCOUNT_ID: 'acct' },
            secretVariables: {
              SLACK_BOT_TOKEN: 'projects/p/secrets/demo-bot/versions/latest',
              R2_ACCESS_KEY_ID: 'projects/p/secrets/demo-r2-id/versions/latest',
            },
          },
        },
      }],
      allocationPolicy: {
        instances: [{ policy: { machineType: 'n2-standard-8', bootDisk: { type: 'pd-balanced', sizeGb: '100' } } }],
        serviceAccount: { email: 'job@p' },
        location: { allowedLocations: ['regions/us-west1'] },
        labels: { app: 'disky', deployment: 'r2', component: 'digest' },
      },
      logsPolicy: { destination: 'CLOUD_LOGGING' },
      labels: { app: 'disky', deployment: 'r2', component: 'digest' },
    }))
  })
})
