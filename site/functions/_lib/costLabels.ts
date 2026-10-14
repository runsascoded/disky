// Cost-attribution labels on the Batch jobs the site submits (specs/cost-labels.md):
// the deployment's `DISKY_LABELS` var (`k=v,k=v`, e.g. `app=disky,deployment=gcs`)
// plus each job's `component`. `allocationPolicy.labels` is what reaches the VMs and
// disks a job creates (their Compute cost lines); the job's own `labels` stay on the
// job. Unset `DISKY_LABELS` = no labels, and the spec is unchanged. The twin of
// `dt_cloud/cost_labels.py`.

const KEY_RE = /^[a-z][a-z0-9_-]{0,62}$/
const VALUE_RE = /^[a-z0-9_-]{0,63}$/

export type Labels = Record<string, string>

const check = (k: string, v: string): void => {
  if (!KEY_RE.test(k)) throw new Error(`label key ${JSON.stringify(k)}: must match ${KEY_RE.source}`)
  if (!VALUE_RE.test(v)) throw new Error(`label ${k}=${JSON.stringify(v)}: value must match ${VALUE_RE.source}`)
}

/** `"k=v,k2=v2"` → `{ k: 'v', k2: 'v2' }`; throws on anything GCP would refuse. */
export function parseLabels(text: string | undefined): Labels {
  const out: Labels = {}
  for (const raw of (text ?? '').split(',')) {
    const item = raw.trim()
    if (!item) continue
    const eq = item.indexOf('=')
    if (eq < 0) throw new Error(`DISKY_LABELS: ${JSON.stringify(item)} is not k=v`)
    const k = item.slice(0, eq).trim(), v = item.slice(eq + 1).trim()
    check(k, v)
    if (k in out) throw new Error(`DISKY_LABELS: duplicate key ${JSON.stringify(k)}`)
    out[k] = v
  }
  if (Object.keys(out).length > 64) throw new Error('DISKY_LABELS: more than 64 labels')
  return out
}

/** `spec` with `labels` + `component` on the job and its allocation policy; `spec` itself when `labels` is empty. */
export function labelBatchSpec<T>(spec: T, labels: Labels | undefined, component: string): T {
  if (!labels || !Object.keys(labels).length) return spec
  check('component', component)
  const all = { ...labels, component }
  const s = spec as { labels?: Labels, allocationPolicy?: { labels?: Labels } }
  return {
    ...s,
    labels: { ...s.labels, ...all },
    allocationPolicy: { ...s.allocationPolicy, labels: { ...s.allocationPolicy?.labels, ...all } },
  } as T
}
