// The executors' Batch settings come from `[vars]` (specs/done/oa-decoupling.md
// step 5): a route answers 503 naming the first key it needs that is unset,
// before it touches GCP.
import { describe, expect, it } from 'vitest'
import { onRequestPost as pdDispatch } from '../api/plan-sweep/dispatch'
import { onRequestGet as pdJobs } from '../api/plan-sweep/jobs'
import { onRequestPost as pdPurge } from '../api/plan-sweep/purge'
import { onRequestPost as pdStop } from '../api/plan-sweep/stop'
import { onRequestPost as pdUndo } from '../api/plan-sweep/undo'
import { onRequestPost as sDispatch } from '../api/sweep/dispatch'
import { onRequestGet as sJobs } from '../api/sweep/jobs'
import { onRequestPost as sStop } from '../api/sweep/stop'
import { batchConfig } from './batchConfig'
import { batchRegionFor, batchRegions } from './gcp'
import { sqliteD1 } from './testD1'

describe('D1', () => {
  describe('batchConfig', () => {
    it('reads every key; the project falls back to the SA key\'s, the region to nothing', () => {
      expect(batchConfig({
        GCP_SA_KEY: JSON.stringify({ project_id: 'key-project' }), DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'img:1',
        CF_ACCOUNT_ID: 'acct', SWEEP_S3_ENDPOINT: 'https://s3.example.com', BUCKET_REGIONS: '{"b1":"us-east1","b2":"europe-west4"}',
        D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db', DISKY_LABELS: 'app=disky,deployment=d',
      }, ['GCP_PROJECT', 'DATA_BUCKET'])).toEqual({
        project: 'key-project', region: '', bucketRegions: { b1: 'us-east1', b2: 'europe-west4' },
        dataBucket: 'my-data', image: 'img:1', cfAccountId: 'acct', s3Endpoint: 'https://s3.example.com',
        d1DbId: 'd1-id', d1DbName: 'my-db', labels: { app: 'disky', deployment: 'd' },
      })
      expect(batchConfig({ GCP_PROJECT: 'p', BATCH_REGION: 'us-east5' }, []))
        .toEqual({ project: 'p', region: 'us-east5', bucketRegions: {}, dataBucket: '', image: '', cfAccountId: '', s3Endpoint: '', d1DbId: '', d1DbName: '', labels: {} })
    })
    it('names the first needed key that is unset', () => {
      expect(batchConfig({}, ['GCP_PROJECT'])).toEqual({ missing: 'GCP_PROJECT' })
      expect(batchConfig({ GCP_SA_KEY: 'not json' }, ['GCP_PROJECT'])).toEqual({ missing: 'GCP_PROJECT' })
      expect(batchConfig({ GCP_PROJECT: 'p' }, ['GCP_PROJECT', 'DATA_BUCKET', 'SWEEP_IMAGE'])).toEqual({ missing: 'DATA_BUCKET' })
      expect(batchConfig({ GCP_PROJECT: 'p', DATA_BUCKET: 'd' }, ['SWEEP_IMAGE', 'CF_ACCOUNT_ID'])).toEqual({ missing: 'SWEEP_IMAGE' })
    })
    it('the region has no default: a route that needs it is told it is unset', () => {
      expect(batchConfig({ GCP_PROJECT: 'p' }, ['GCP_PROJECT', 'BATCH_REGION'])).toEqual({ missing: 'BATCH_REGION' })
      expect(batchConfig({ GCP_PROJECT: 'p', BATCH_REGION: '' }, ['GCP_PROJECT', 'BATCH_REGION', 'DATA_BUCKET'])).toEqual({ missing: 'BATCH_REGION' })
      expect(batchConfig({ GCP_PROJECT: 'p', BATCH_REGION: 'us-east5' }, ['GCP_PROJECT', 'BATCH_REGION', 'DATA_BUCKET'])).toEqual({ missing: 'DATA_BUCKET' })
    })
    it('a one-region cut runs there; a mixed or unmapped cut in the deployment\'s region; the regions to list', () => {
      const regions = '{"b1":"us-east1","b2":"europe-west4","b3":"us-east1"}'
      const cuts = [['b1'], ['b1', 'b3'], ['b1', 'b2'], ['zz']]
      const cfg = batchConfig({ GCP_PROJECT: 'p', BATCH_REGION: 'us-west4', BUCKET_REGIONS: regions }, []) as never
      expect(cuts.map(c => batchRegionFor(cfg, c))).toEqual(['us-east1', 'us-east1', 'us-west4', 'us-west4'])
      expect(batchRegions(cfg)).toEqual(['us-west4', 'us-east1', 'europe-west4'])
      // no BATCH_REGION: a cut in no one region has nowhere to run ('', which its caller refuses), and nothing extra is listed
      const bare = batchConfig({ GCP_PROJECT: 'p', BUCKET_REGIONS: regions }, []) as never
      expect(cuts.map(c => batchRegionFor(bare, c))).toEqual(['us-east1', 'us-east1', '', ''])
      expect(batchRegions(bare)).toEqual(['us-east1', 'europe-west4'])
      expect(batchRegions(batchConfig({ GCP_PROJECT: 'p' }, []) as never)).toEqual([])
    })
  })

  // Every key a route needs, set; each case drops one.
  const FULL = {
    GCP_SA_KEY: '{}', JOB_SA: 'job@my-project.iam.gserviceaccount.com', GCP_PROJECT: 'my-project', BATCH_REGION: 'my-region', DATA_BUCKET: 'my-data',
    SWEEP_IMAGE: 'img:1', CF_ACCOUNT_ID: 'acct', SWEEP_S3_ENDPOINT: 'https://s3.example.com',
    D1_DB_ID: 'd1-id', D1_DB_NAME: 'my-db',
    STORE_SCHEME: 's3://', STORE_BUCKETS: 'b1,b2',
  }
  const post = (path: string, body: unknown) => new Request(`http://localhost${path}`, { method: 'POST', body: JSON.stringify(body) })
  const without = async (key: string) => {
    const { db } = await sqliteD1()
    const env: Record<string, unknown> = { ...FULL, DB: db }
    delete env[key]
    return env as never
  }
  const answer = async (r: Response) => [r.status, ((await r.json()) as { error: string }).error]

  describe('each executor route 503s, naming the unset key', () => {
    const dispatch = { plan_id: 1, mode: 'dry', date: '2026-10-01' }
    it('plan-sweep (the plan-first executor)', async () => {
      expect(await Promise.all([
        pdDispatch({ request: post('/api/plan-sweep/dispatch', dispatch), env: await without('DATA_BUCKET') } as never).then(answer),
        pdDispatch({ request: post('/api/plan-sweep/dispatch', dispatch), env: await without('SWEEP_S3_ENDPOINT') } as never).then(answer),
        pdDispatch({ request: post('/api/plan-sweep/dispatch', dispatch), env: await without('STORE_BUCKETS') } as never).then(answer),
        pdUndo({ request: post('/api/plan-sweep/undo', { run_id: 'x' }), env: await without('SWEEP_IMAGE') } as never).then(answer),
        pdPurge({ request: post('/api/plan-sweep/purge', { run_id: 'x' }), env: await without('GCP_PROJECT') } as never).then(answer),
        pdStop({ request: post('/api/plan-sweep/stop', { job_id: 'x' }), env: await without('GCP_PROJECT') } as never).then(answer),
        pdJobs({ request: new Request('http://localhost/api/plan-sweep/jobs'), env: await without('DATA_BUCKET') } as never).then(answer),
        // every plan-sweep job runs, and is listed and cancelled, in the deployment's region: unset, nothing is submitted
        pdDispatch({ request: post('/api/plan-sweep/dispatch', dispatch), env: await without('BATCH_REGION') } as never).then(answer),
        pdUndo({ request: post('/api/plan-sweep/undo', { run_id: 'x' }), env: await without('BATCH_REGION') } as never).then(answer),
        pdPurge({ request: post('/api/plan-sweep/purge', { run_id: 'x' }), env: await without('BATCH_REGION') } as never).then(answer),
        pdStop({ request: post('/api/plan-sweep/stop', { job_id: 'x' }), env: await without('BATCH_REGION') } as never).then(answer),
        pdJobs({ request: new Request('http://localhost/api/plan-sweep/jobs'), env: await without('BATCH_REGION') } as never).then(answer),
      ])).toEqual([
        [503, 'dispatch not configured (DATA_BUCKET unset)'],
        [503, 'dispatch not configured (SWEEP_S3_ENDPOINT unset)'],
        [503, 'dispatch not configured (STORE_BUCKETS unset)'],
        [503, 'undo not configured (SWEEP_IMAGE unset)'],
        [503, 'purge not configured (GCP_PROJECT unset)'],
        [503, 'stop not configured (GCP_PROJECT unset)'],
        [503, 'jobs not configured (DATA_BUCKET unset)'],
        [503, 'dispatch not configured (BATCH_REGION unset)'],
        [503, 'undo not configured (BATCH_REGION unset)'],
        [503, 'purge not configured (BATCH_REGION unset)'],
        [503, 'stop not configured (BATCH_REGION unset)'],
        [503, 'jobs not configured (BATCH_REGION unset)'],
      ])
    })
    it('sweep (the multi-bucket executor)', async () => {
      expect(await Promise.all([
        sDispatch({ request: post('/api/sweep/dispatch', dispatch), env: await without('CF_ACCOUNT_ID') } as never).then(answer),
        sDispatch({ request: post('/api/sweep/dispatch', dispatch), env: await without('SWEEP_IMAGE') } as never).then(answer),
        sDispatch({ request: post('/api/sweep/dispatch', dispatch), env: await without('STORE_BUCKETS') } as never).then(answer),
        sStop({ request: post('/api/sweep/stop', { job_id: 'x' }), env: await without('DATA_BUCKET') } as never).then(answer),
        sJobs({ request: new Request('http://localhost/api/sweep/jobs'), env: await without('GCP_PROJECT') } as never).then(answer),
      ])).toEqual([
        [503, 'dispatch not configured (CF_ACCOUNT_ID unset)'],
        [503, 'dispatch not configured (SWEEP_IMAGE unset)'],
        [503, 'dispatch not configured (STORE_BUCKETS unset)'],
        [503, 'stop not configured (DATA_BUCKET unset)'],
        [503, 'jobs not configured (GCP_PROJECT unset)'],
      ])
    })
    it('a sweep cut may name only scanned buckets', async () => {
      const { db } = await sqliteD1()
      const env = { ...FULL, DB: db } as never
      expect(await sDispatch({ request: post('/api/sweep/dispatch', { ...dispatch, buckets: ['b9'] }), env } as never).then(answer))
        .toEqual([400, 'bad bucket name'])
    })
    it('a sweep run names a known machine, and dispatch needs the D1 its runs record to', async () => {
      const { db } = await sqliteD1()
      expect(await Promise.all([
        sDispatch({ request: post('/api/sweep/dispatch', { ...dispatch, machine: 'n2-highmem-96' }), env: { ...FULL, DB: db } } as never).then(answer),
        sDispatch({ request: post('/api/sweep/dispatch', dispatch), env: await without('D1_DB_ID') } as never).then(answer),
      ])).toEqual([
        [400, 'machine must be one of n2-highmem-8, n2-highmem-32'],
        [503, 'dispatch not configured (D1_DB_ID unset)'],
      ])
    })
  })
})
