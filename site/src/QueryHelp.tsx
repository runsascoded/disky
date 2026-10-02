import { MdHelpOutline } from 'react-icons/md'
import type { QuerySyntax } from '../functions/_lib/queryAst'
import { Tooltip } from './Tooltip'

/** The filter box's help card: the active syntax's `describe()` — a picker
 * over the registered syntaxes, its summary, one row per form with an
 * example, its notes. Generated, so a new syntax brings its own help. */
export function QueryHelpCard({ syntaxes, active, onPick }: {
  syntaxes: readonly QuerySyntax[]
  active: QuerySyntax
  /** Pick a syntax (the page's `?qs=`); without it the picker is plain text. */
  onPick?: (id: string) => void
}) {
  const help = active.describe()
  return (
    <div className="qhelp-card">
      <div className="qhelp-head">
        <span>Filter syntax:</span>
        {syntaxes.map(s => onPick
          ? <button key={s.id} type="button" className={s === active ? 'on' : ''} aria-pressed={s === active} onClick={() => onPick(s.id)}>{s.describe().label}</button>
          : s === active && <b key={s.id}>{help.label}</b>)}
      </div>
      <p>{help.summary}</p>
      <table>
        <tbody>
          {help.forms.map(f => (
            <tr key={f.form}><td><code>{f.form}</code></td><td>{f.meaning}</td><td><code>{f.example}</code></td></tr>
          ))}
        </tbody>
      </table>
      {help.notes.map(n => <p key={n} className="qhelp-note">{n}</p>)}
    </div>
  )
}

/** The `?` beside the filter box: hover / focus shows the card, a click pins
 * it (so the syntax picker is reachable). */
export function QueryHelpTip(props: Parameters<typeof QueryHelpCard>[0]) {
  return (
    <Tooltip pinnable placement="bottom-end" content={<QueryHelpCard {...props} />}>
      <span className="qhelp" aria-label="filter syntax help"><MdHelpOutline /></span>
    </Tooltip>
  )
}
