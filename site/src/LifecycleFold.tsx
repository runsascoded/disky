import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { describeRule, displayId, groupRows, lifecycleDiffByBucket, parseLifecycle, rulePrefix } from './lifecycle'
import type { BucketLifecycleRow, LifecycleSnapshot } from './lifecycle'
import { storeUrl, type Store } from './stores'
import { Tooltip } from './Tooltip'

// Each bucket's lifecycle rules as the scan job snapshotted them
// (`<store.base>/<scan>/lifecycle.json`), diffed per bucket against the
// previous scan's snapshot: a `<details>` fold under About. The store's
// `lifecycle` config picks the rule adapter (S3 `Rules[]` or GCS
// `{action, condition}`), whether rows a rule holds for across buckets fold
// into one (a fleet sharing its rules) or list per bucket, and the first scan
// with a file — earlier scans say so rather than fetching a 404.

const PREFIX_CLIP = 36

/** One scan's rules; `null` = no snapshot for that scan (404 or any non-OK). */
function useLifecycle(store: Store, scan: string | null | undefined) {
  const lc = store.lifecycle
  return useQuery<LifecycleSnapshot | null>({
    queryKey: ['lifecycle', store.key, scan],
    enabled: !!lc && !!scan && (!lc.recordedFrom || scan >= lc.recordedFrom),
    staleTime: Infinity,
    queryFn: async () => {
      const r = await fetch(storeUrl(`${store.base}/${scan}/lifecycle.json`, store))
      return r.ok ? parseLifecycle(await r.json(), store.buckets[0], lc!.rules) : null
    },
  })
}

/** A changed rule's previous version, on the chip. */
function ChangedChip({ rule, prev }: { rule: BucketLifecycleRow['rule']; prev: BucketLifecycleRow['rule'] }) {
  return (
    <Tooltip content={<>
      Previous scan: <b>{describeRule(prev)}</b>
      {prev.Status !== rule.Status && <> · {prev.Status}</>}
      {rulePrefix(prev) !== rulePrefix(rule) && <> · <code>{rulePrefix(prev) || 'whole bucket'}</code></>}
    </>}>
      <span className="chip changed">changed</span>
    </Tooltip>
  )
}

function RuleCells({ rule, change, prev }: Pick<BucketLifecycleRow, 'rule' | 'change' | 'prev'>) {
  const p = rulePrefix(rule)
  // A long prefix (a deep scratch path) clips with the whole value on hover, so the action column stays in view.
  const prefix = !p ? <i>whole bucket</i>
    : p.length > PREFIX_CLIP ? <Tooltip content={<code className="elide-full">{p}</code>}><code className="clip">{p}</code></Tooltip>
    : <code>{p}</code>
  return (
    <>
      <td className="id">
        {displayId(rule)}
        {change === 'new' && <span className="chip new">new</span>}
        {change === 'removed' && <span className="chip removed">removed</span>}
        {change === 'changed' && prev && <ChangedChip rule={rule} prev={prev} />}
      </td>
      <td>{prefix}</td>
      <td>{describeRule(rule)}</td>
      <td>{rule.Status}</td>
    </>
  )
}

export function LifecycleFold({ store, asof, prevScan, note }: {
  store: Store
  asof: string | null | undefined
  /** The scan before `asof` in the store's list (the diff baseline), if any. */
  prevScan: string | null | undefined
  /** One line under the table — where this deployment tracks the intended state. */
  note?: ReactNode
}) {
  const curQ = useLifecycle(store, asof)
  const prevQ = useLifecycle(store, prevScan)
  const rules = curQ.data
  const rows = rules ? lifecycleDiffByBucket(prevQ.data ?? null, rules) : []
  const grouped = !!store.lifecycle?.grouped
  const loading = !!asof && curQ.isPending
  const nRules = rules ? Object.values(rules).reduce((n, rs) => n + rs.length, 0) : 0
  const nBuckets = rules ? Object.keys(rules).length : 0
  const multi = nBuckets > 1
  const title = rules
    ? `${nRules} rule${nRules === 1 ? '' : 's'}${multi ? ` · ${nBuckets} buckets` : ''}`
    : loading ? 'loading…' : 'no snapshot'
  const bucketsCell = (b: string[]) => (b.length === nBuckets ? <i>all {nBuckets}</i> : b.join(', '))
  return (
    <details className="prose fold lifecycle">
      <summary>
        <span><b>Bucket lifecycle</b> — {title}</span>
      </summary>
      {rules ? (
        <div className="tbl-scroll">
        <table className="worklist lifecycle-tbl">
          <thead>
            <tr>
              {multi && !grouped && <th>bucket</th>}
              <th>rule</th>
              {multi && grouped && <th>buckets</th>}
              <th>prefix</th><th>action</th><th>status</th>
            </tr>
          </thead>
          <tbody>
            {grouped
              ? groupRows(rows).map(({ rule, change, prev, buckets }) => (
                <tr key={`${rule.ID}:${change ?? ''}:${buckets.join(',')}`} className={change === 'removed' ? 'removed' : undefined}>
                  <RuleCells rule={rule} change={change} prev={prev} />
                  {multi && <td className="buckets">{bucketsCell(buckets)}</td>}
                </tr>
              ))
              : rows.map(({ bucket, rule, change, prev }, i) => {
                // The bucket cell shows once per run of rows, with a divider above each new bucket.
                const firstOfBucket = i === 0 || rows[i - 1].bucket !== bucket
                const cls = [change === 'removed' ? 'removed' : '', firstOfBucket && i > 0 ? 'bucket-start' : ''].filter(Boolean).join(' ') || undefined
                return (
                  <tr key={`${bucket}/${rule.ID}:${change ?? ''}`} className={cls}>
                    {multi && <td className="id bucket">{firstOfBucket ? bucket : ''}</td>}
                    <RuleCells rule={rule} change={change} prev={prev} />
                  </tr>
                )
              })}
          </tbody>
        </table>
        </div>
      ) : (
        <p className="tab-note">{loading ? 'loading…' : <>no snapshot for this scan{store.lifecycle?.recordedFrom && <> (recorded from {store.lifecycle.recordedFrom} on)</>}</>}</p>
      )}
      {rules && prevScan && prevQ.data === null && <p className="tab-note">Changes aren’t shown: the previous scan has no snapshot.</p>}
      {note && <p className="tab-note">{note}</p>}
    </details>
  )
}
