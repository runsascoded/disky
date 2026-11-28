// Which backend answered the page's map requests (VM mode beside the indexes, specs/vm-pilot.md step 4): `vm` (`vmd`),
// `idx` (today's indexes), or `idx*` (a `vm` session answered by the indexes, with why). Shown only once an answer
// has said (VM mode on); its tip lists each request kind's latest answer and re-pins the session.
import { useSyncExternalStore } from 'react'
import { type Answered, type Backend, backendStore, headline, KINDS, repinHref, type Seen } from './backend'
import { Tooltip } from './Tooltip'

const LABEL: Record<Answered, string> = { vm: 'vm', idx: 'idx', 'idx-fallback': 'idx*' }
const SAYS: Record<Answered, string> = { vm: 'vmd (VM mode)', idx: 'the indexes', 'idx-fallback': 'the indexes, in place of vmd' }

function BackendTip({ pin, seen }: { pin: Backend | null; seen: Record<string, Seen> }) {
  const other: Backend = pin === 'vm' ? 'idx' : 'vm'
  return (
    <div className="be-tip">
      <p>This session is pinned to <b>{pin ?? 'idx'}</b>{pin ? '' : ' (the default)'}: <code>vm</code> is answered by <code>vmd</code>, the VM&apos;s in-memory server; <code>idx</code> by today&apos;s indexes.</p>
      <ul>
        {Object.entries(KINDS).filter(([k]) => seen[k]).map(([k, name]) => (
          <li key={k}><b>{name}</b>: {SAYS[seen[k].be]}{seen[k].note ? <> &mdash; {seen[k].note}</> : null}</li>
        ))}
      </ul>
      <a href={repinHref(location.href, other)}>Pin this session to {other}</a>
    </div>
  )
}

export function BackendBadge() {
  const { pin, seen } = useSyncExternalStore(backendStore.subscribe, backendStore.get)
  const head = headline(seen)
  if (!head) return null
  return (
    <Tooltip content={<BackendTip pin={pin} seen={seen} />} placement="bottom-end">
      <span className={`be-badge be-${head.be}`} role="status" aria-label={`answered by ${SAYS[head.be]}`}>{LABEL[head.be]}</span>
    </Tooltip>
  )
}
