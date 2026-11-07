import { describe, expect, it } from 'vitest'
import { batchConfig } from './batchConfig.js'
import { sweepBatchSpec } from './cwBatch.js'
import { labelBatchSpec, parseLabels } from './costLabels.js'
import { sweepJobSpec } from './sweepDispatch.js'

const DEPLOY = { app: 'disky', deployment: 'gcs' }
type Labeled = { labels?: Record<string, string>, allocationPolicy: { labels?: Record<string, string> } }
const labelsOf = (spec: unknown) => [(spec as Labeled).labels, (spec as Labeled).allocationPolicy.labels]

describe('cost labels: DISKY_LABELS + a component, on the job and its VMs', () => {
  it('parses k=v lists, refusing what GCP would', () => {
    expect(parseLabels(undefined)).toEqual({})
    expect(parseLabels(' app=disky,,deployment=gcs ')).toEqual(DEPLOY)
    expect(() => parseLabels('app')).toThrow('DISKY_LABELS: "app" is not k=v')
    expect(() => parseLabels('App=x')).toThrow('label key "App": must match ^[a-z][a-z0-9_-]{0,62}$')
    expect(() => parseLabels('app=a,app=b')).toThrow('DISKY_LABELS: duplicate key "app"')
  })
  it('no labels: the spec itself', () => {
    const spec = { allocationPolicy: {} }
    expect(labelBatchSpec(spec, {}, 'sweep')).toBe(spec)
    expect(labelBatchSpec(spec, undefined, 'sweep')).toBe(spec)
  })
  it('merges onto the job and its allocation policy', () => {
    expect(labelBatchSpec({ labels: { purpose: 'x' }, allocationPolicy: { serviceAccount: { email: 'sa' } } }, DEPLOY, 'sweep')).toEqual({
      labels: { purpose: 'x', ...DEPLOY, component: 'sweep' },
      allocationPolicy: { serviceAccount: { email: 'sa' }, labels: { ...DEPLOY, component: 'sweep' } },
    })
  })
  it('batchConfig parses DISKY_LABELS; both executors carry them', () => {
    const cfg = batchConfig({ GCP_PROJECT: 'p', DATA_BUCKET: 'd', SWEEP_IMAGE: 'i', DISKY_LABELS: 'app=disky,deployment=gcs' }, [])
    if ('missing' in cfg) throw new Error(cfg.missing)
    expect(cfg.labels).toEqual(DEPLOY)
    const base = { cfg, jobSa: 'sa', region: 'us-east1', script: 'true', actor: 'a', siteUrl: 'https://s', env: {} }
    const sweep = { ...DEPLOY, component: 'sweep' }, undo = { ...DEPLOY, component: 'sweep-undo' }
    expect(labelsOf(sweepJobSpec(base))).toEqual([sweep, sweep])
    expect(labelsOf(sweepJobSpec({ ...base, component: 'sweep-undo' }))).toEqual([undo, undo])
    expect(labelsOf(sweepBatchSpec(cfg, 'sa', 'true', 'b'))).toEqual([sweep, sweep])
    expect(labelsOf(sweepBatchSpec(cfg, 'sa', 'true', 'b', {}, {}, 'sweep-undo'))).toEqual([undo, undo])
  })
})
