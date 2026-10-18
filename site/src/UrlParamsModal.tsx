// "URL params": what this page's query string means (use-prms reflection: the hooks mounted here, plus the
// `urlParams.ts` catalogue for the rest), so a link's `?d=…&ps=scan` explains itself.
import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { type ParamReflection, type UnknownParam, useParamReflection } from 'use-prms'
import { URL_PARAMS } from './urlParams'

export interface ParamGroups {
  /** In the URL now. */
  set: ParamReflection[]
  /** Read on this page, absent (their default). */
  here: ParamReflection[]
  /** Known (the catalogue), read on other pages. */
  elsewhere: ParamReflection[]
  /** In the URL, read by nothing. */
  unknown: UnknownParam[]
}

const byKey = (a: { key: string }, b: { key: string }) => a.key.localeCompare(b.key)

/** Reflected params by where they stand: set, read here, read elsewhere; and the URL's unrecognized keys. An absent
 *  `hidden` key (a legacy spelling, a dev fixture's) is left out. */
export function groupParams(params: ParamReflection[], unknown: UnknownParam[], hidden: (key: string) => boolean = () => false): ParamGroups {
  const ps = [...params].sort(byKey)
  const shown = ps.filter(p => p.raw !== undefined || !hidden(p.key))
  return {
    set: shown.filter(p => p.raw !== undefined),
    here: shown.filter(p => p.raw === undefined && p.refs > 0),
    elsewhere: shown.filter(p => p.raw === undefined && p.refs === 0),
    unknown: [...unknown].sort(byKey),
  }
}

/** `search` without `keys`. */
export function withoutKeys(search: string, keys: string[]): string {
  const q = new URLSearchParams(search)
  for (const k of keys) q.delete(k)
  const s = q.toString()
  return s ? `?${s}` : ''
}

/** `text` with its backticked spans as `<code>`. */
export const withCode = (text: string): (string | { code: string })[] =>
  text.split('`').map((s, i) => (i % 2 ? { code: s } : s)).filter(s => s !== '')

function Row({ p, onRemove }: { p: ParamReflection; onRemove?: () => void }) {
  const label = p.label ?? p.key
  const pages = URL_PARAMS[p.key]?.pages
  return (
    <li className={`up-row up-${p.state}`}>
      <div className="up-line">
        <code className="up-key">{p.liveKey ?? p.key}</code>
        <span className="up-label">{label !== p.key ? label : null}</span>
        {p.raw !== undefined
          ? <code className="up-val">{p.raw === '' ? '(flag)' : p.raw}</code>
          : p.examples?.length ? <span className="up-ex">e.g. {p.examples.map(e => <code key={e}>{e}</code>)}</span> : null}
        {p.state === 'stale' && <span className="up-note">→ <code>{p.canonical}</code></span>}
        {p.state === 'malformed' && <span className="up-note">unreadable</span>}
        {pages && <span className="up-pages">{pages.join(' · ')}</span>}
        {onRemove && <button type="button" className="up-rm" onClick={onRemove} aria-label={`Remove ${p.key}`}>remove</button>}
      </div>
      {typeof p.description === 'string' && <div className="up-desc">{withCode(p.description).map((s, i) => typeof s === 'string' ? s : <code key={i}>{s.code}</code>)}</div>}
    </li>
  )
}

export function UrlParamsModal({ onClose }: { onClose: () => void }) {
  const { params, unknown } = useParamReflection()
  const g = groupParams(params, unknown, k => !!URL_PARAMS[k]?.hidden)
  const { pathname, search, hash } = useLocation()
  const navigate = useNavigate()
  const remove = (keys: string[]) => navigate({ pathname, search: withoutKeys(search, keys), hash })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="token-backdrop" onClick={onClose}>
      <div className="token-modal about-modal url-params" role="dialog" aria-label="URL params" onClick={e => e.stopPropagation()}>
        <div className="token-head">
          <strong>URL params</strong>
          <button type="button" className="token-x" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <h3>In this link</h3>
        {g.set.length || g.unknown.length ? (
          <ul className="up-list">
            {g.set.map(p => <Row key={p.key} p={p} onRemove={() => remove(p.keys)} />)}
            {g.unknown.map(u => (
              <li key={u.key} className="up-row up-unknown">
                <div className="up-line">
                  <code className="up-key">{u.key}</code>
                  <span className="up-label">not recognized</span>
                  <code className="up-val">{u.raw}</code>
                  <button type="button" className="up-rm" onClick={() => remove([u.key])} aria-label={`Remove ${u.key}`}>remove</button>
                </div>
              </li>
            ))}
          </ul>
        ) : <p className="up-none">None: every setting is at its default.</p>}
        {g.here.length > 0 && <>
          <h3>Also read on this page</h3>
          <ul className="up-list">{g.here.map(p => <Row key={p.key} p={p} />)}</ul>
        </>}
        {g.elsewhere.length > 0 && <>
          <h3>On other pages</h3>
          <ul className="up-list">{g.elsewhere.map(p => <Row key={p.key} p={p} />)}</ul>
        </>}
      </div>
    </div>
  )
}
