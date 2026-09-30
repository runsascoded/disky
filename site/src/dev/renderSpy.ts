// Render spy: counts React commits and, per commit, which components rendered
// and how long each took — so a test (or a hand audit in the browser console)
// can assert "this click caused exactly one commit, rendering exactly these
// components". Reads React's profiling fields off the fiber tree through the
// same hook React DevTools uses; installs a minimal hook when no DevTools is
// present (so it works headless), wraps the real one when it is.
//
// Must be imported BEFORE react-dom (React binds to the hook at load), which
// is why `main.tsx` imports it first. Active in dev builds and whenever the
// page is loaded with `?spy=1`; otherwise a no-op.

export interface RenderCommit {
  /** `performance.now()` at commit. */
  t: number
  /** Component fibers that rendered in this commit. */
  rendered: number
  /** Sum of those components' own (non-child) render time, ms. */
  selfMs: number
  /** Per component name: renders in this commit + own render time. */
  components: Record<string, { count: number; ms: number }>
  /** Components whose state update scheduled this commit (React's DEV
   *  updater tracking; empty for a commit no component scheduled). */
  updaters: string[]
}

export interface RenderSpy {
  commits: RenderCommit[]
  reset(): void
  /** Sorted, deduped component names per commit since the last `reset()`. */
  summary(): { rendered: number; selfMs: number; components: string[]; updaters: string[] }[]
}

declare global {
  interface Window {
    __renderSpy?: RenderSpy
    __REACT_DEVTOOLS_GLOBAL_HOOK__?: any
  }
}

// Function / class / forwardRef / memo / simple-memo component fibers.
const COMPONENT_TAGS = new Set([0, 1, 11, 14, 15])
const PERFORMED_WORK = 0b1

function fiberName(f: any): string {
  const t = f.type
  return t?.displayName || t?.name || t?.render?.displayName || t?.render?.name || t?.type?.displayName || t?.type?.name || `tag${f.tag}`
}

function install() {
  const spy: RenderSpy = {
    commits: [],
    reset() { spy.commits.length = 0; last = performance.now() },
    summary() {
      return spy.commits.map(c => ({ rendered: c.rendered, selfMs: +c.selfMs.toFixed(1), components: Object.keys(c.components).sort(), updaters: c.updaters }))
    },
  }
  let last = performance.now()
  const onCommit = (root: any) => {
    const since = last
    const components: RenderCommit['components'] = {}
    let rendered = 0, selfMs = 0
    const walk = (f: any) => {
      for (let c = f; c; c = c.sibling) {
        // Rendered this commit = React visited it this commit (a start time
        // after the last commit; untouched subtrees keep their old fibers and
        // old start times) AND its render function ran (`PerformedWork`; an
        // ancestor on the path to an update is visited but bails out).
        if (c.actualStartTime >= since && (c.flags & PERFORMED_WORK) && COMPONENT_TAGS.has(c.tag)) {
          const n = fiberName(c)
          const self = c.selfBaseDuration || 0
          const e = (components[n] ??= { count: 0, ms: 0 })
          e.count++; e.ms += self; rendered++; selfMs += self
        }
        if (c.child) walk(c.child)
      }
    }
    walk(root.current.child)
    const updaters = [...(root.memoizedUpdaters ?? [])].map(fiberName).sort()
    spy.commits.push({ t: performance.now(), rendered, selfMs, components, updaters })
    last = performance.now()
  }
  const existing = window.__REACT_DEVTOOLS_GLOBAL_HOOK__
  if (existing) {
    const orig = existing.onCommitFiberRoot?.bind(existing)
    existing.onCommitFiberRoot = (id: number, root: any, ...rest: unknown[]) => { onCommit(root); return orig?.(id, root, ...rest) }
  } else {
    window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
      supportsFiber: true,
      renderers: new Map(),
      inject: () => 1,
      onCommitFiberRoot: (_id: number, root: any) => onCommit(root),
      onCommitFiberUnmount: () => {},
      onPostCommitFiberRoot: () => {},
      checkDCE: () => {},
    }
  }
  window.__renderSpy = spy
}

if (typeof window !== 'undefined' && (import.meta.env.DEV || new URLSearchParams(location.search).get('spy') === '1')) install()
