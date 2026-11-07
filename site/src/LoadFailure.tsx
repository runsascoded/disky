/** A failed load stated inline where its widget would be (the map, the diff, the size chart): a filter refusal's
 *  reason as is (`filterCaps.ts`), anything else as what failed and the server's message, with a retry when a
 *  retry may pass (a 5xx, or no response). Never a blank or endlessly "loading" slot. */
import { ApiError, failureOf } from './filterCaps'

export function LoadFailure({ err, what, onRetry, as: Tag = 'p', className = 'loading' }: {
  err: unknown
  /** What failed, as the message's lead ("view", "diff", "series"). */
  what: string
  onRetry?: () => void
  /** The element: a paragraph in a slot of its own, a span inside a line. */
  as?: 'p' | 'span'
  className?: string
}) {
  const f = failureOf(err)
  if ('refusal' in f) return <Tag className={`${className} filter-refused`} role="status">{f.refusal}</Tag>
  return (
    <Tag className={`${className} load-failed`} role="alert">
      {what} failed: {f.message}
      {f.retry && onRetry && <>{' '}<button type="button" className="linkish" onClick={onRetry}>retry</button></>}
    </Tag>
  )
}

/** A level of the drilled path that didn't load (`spine.ts` `spineFailure`): a path the scan doesn't hold (a 404) as
 *  that, a filter refusal as its reason, anything else as the server's message, with a retry when a retry may pass (a
 *  5xx, or no response). Over the map, when it draws the deepest ancestor that loaded instead (`shown`); in the
 *  map's place, when all it has is the failed level's own node, empty (no `shown`). */
export function LevelFailure({ path, err, shown, onRetry }: { path: string; err: unknown; shown?: string; onRetry?: () => void }) {
  const f = failureOf(err)
  const instead = shown != null && <>{' · '}showing <code>{shown}</code></>
  if ('refusal' in f) return <p className="level-failed filter-refused" role="status"><code>{path}</code>: {f.refusal}{instead}</p>
  if (err instanceof ApiError && err.status === 404) return <p className="level-failed" role="status"><code>{path}</code> isn’t in this scan{instead}</p>
  return (
    <p className="level-failed load-failed" role="alert">
      couldn’t load <code>{path}</code>: {f.message}{instead}
      {f.retry && onRetry && <>{' '}<button type="button" className="linkish" onClick={onRetry}>retry</button></>}
    </p>
  )
}

/** What the map slot shows: the asked-for tree (`map`); else its failure (`failed`: any non-2xx once its retries
 *  are spent — before a held tree, which is another scan's or scope's and would sit dimmed under "loading"
 *  forever); else the last tree, dimmed while the asked-for one loads (`held`); else the first-paint skeleton. */
export function mapSlot(tree: unknown, held: unknown, err: unknown): 'map' | 'failed' | 'held' | 'skeleton' {
  return tree ? 'map' : err ? 'failed' : held ? 'held' : 'skeleton'
}
