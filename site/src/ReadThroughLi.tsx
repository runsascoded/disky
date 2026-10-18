import { type ReadThrough, readThroughText } from './readLens'
import { Tooltip } from './Tooltip'

const tip = (through: ReadThrough, gap: string | null) =>
  `Access logs arrive hours after the requests they record. This scan's read data includes requests through ${through.label} (UTC)${gap ? '; reads after that are provisional: they may have happened, and will show in a later scan' : ''}.`

/** The read window's end, as a legend item: "last access data: <ts>", and, when the scan was taken after
 *  that hour, the gap marked provisional (spec `access-log-freshness.md` §B). */
export function ReadThroughLi({ through }: { through: ReadThrough }) {
  const { through: text, gap } = readThroughText(through)
  return (
    <Tooltip content={tip(through, gap)}>
      <span className="li has-tt read-through">{text}{gap && <span className="provisional"> · {gap}</span>}</span>
    </Tooltip>
  )
}

/** The same, as the banner over the map in read mode. */
export function ReadThroughBanner({ through }: { through: ReadThrough }) {
  const { through: text, gap } = readThroughText(through)
  return (
    <Tooltip content={tip(through, gap)}>
      <div className="read-through-banner" role="note" tabIndex={0}>{text}{gap && <span className="provisional"> · {gap}</span>}</div>
    </Tooltip>
  )
}
