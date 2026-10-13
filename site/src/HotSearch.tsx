import { useState } from 'react'
import { HOT_DATES, changeHotDraft, exactInteger, hotDraft, hotDraftParams, type HotDraft, type HotResult } from './hotModel'

export function HotSearchFields({ draft, onChange, dates }: { draft: HotDraft; onChange: (field: keyof HotDraft, value: string) => void; dates?: readonly string[] }) {
  const { date, name, from } = draft
  const scans = dates ?? HOT_DATES
  return <>
    <label className="hot-literal">Name contains<input name="name" value={name} onChange={event => onChange('name', event.target.value)} required aria-describedby="hot-availability" /></label>
    <label>Scan<select name="date" value={date} onChange={event => onChange('date', event.target.value)}>{!scans.some(day => day === date) && <option value={date}>{date} (unavailable)</option>}{scans.map(day => <option key={day}>{day}</option>)}</select></label>
    <label>Baseline scan (optional)<select name="from" value={from} onChange={event => onChange('from', event.target.value)}><option value="">No baseline</option>{dates ? <>
      {from && (!dates.some(day => day === from) || from >= date) && <option value={from}>{from} (unavailable)</option>}
      {dates.filter(day => day < date).map(day => <option key={day}>{day}</option>)}
    </> : <>{from && from !== HOT_DATES[0] && <option value={from}>{from} (unavailable)</option>}<option value={HOT_DATES[0]} disabled={date !== HOT_DATES[1]}>{HOT_DATES[0]}</option></>}</select></label>
  </>
}

export function HotSearchForm({ params, onSearch, dates }: { params: URLSearchParams; onSearch: (next: URLSearchParams) => void; dates?: readonly string[] }) {
  const [draft, setDraft] = useState(() => hotDraft(params))
  return <form autoComplete="off" onSubmit={event => {
    event.preventDefault()
    onSearch(hotDraftParams(draft))
  }}>
    <HotSearchFields draft={draft} dates={dates} onChange={(field, value) => setDraft(current => changeHotDraft(current, field, value))} />
    <button type="submit">Search</button>
  </form>
}

export function HotTotals({ result }: { result: HotResult }) {
  const { before, after, delta } = result
  const rows = [{ path: 'All buckets', after: after.root, before: before?.root, delta },
    ...after.buckets.map((row, i) => ({ path: row.path, after: row, before: before?.buckets[i],
      delta: before ? { b: row.b - before.buckets[i].b, o: row.o - before.buckets[i].o } : undefined }))]
  return <div className="hot-table-scroll" tabIndex={0} aria-label="Exact fleet and bucket totals">
    <table><caption>Matching coverage for “{after.pattern}”{before ? ` from ${before.date} to ${after.date}` : ` on ${after.date}`}</caption>
      <thead>{before ? <>
        <tr><th rowSpan={2} scope="col">Scope</th><th colSpan={2} scope="colgroup">{before.date}</th><th colSpan={2} scope="colgroup">{after.date}</th><th colSpan={2} scope="colgroup">Change</th></tr>
        <tr>{['before', 'after', 'change'].flatMap(key => [<th key={key + 'b'} scope="col">Bytes</th>, <th key={key + 'o'} scope="col">Objects</th>])}</tr>
      </> : <tr><th scope="col">Scope</th><th scope="col">Bytes</th><th scope="col">Objects</th></tr>}</thead>
      <tbody>{rows.map((row, i) => <tr key={row.path} className={i === 0 ? 'hot-fleet' : undefined}>
        <th scope="row">{row.path}</th>
        {before && <><td>{exactInteger(row.before!.b)}</td><td>{exactInteger(row.before!.o)}</td></>}
        <td>{exactInteger(row.after.b)}</td><td>{exactInteger(row.after.o)}</td>
        {before && <><td>{exactInteger(row.delta!.b, true)}</td><td>{exactInteger(row.delta!.o, true)}</td></>}
      </tr>)}</tbody>
    </table>
  </div>
}
