// The laptop's scan schedule (the disky app's `settings.schedule`: local HH:MM slots in its
// time zone, reported through the tunnel's health check) → when the next scan is due.

const HHMM = /^([01]\d|2[0-3]):([0-5]\d)$/

/** `tz`'s offset from UTC at instant `t`, in ms (positive east of Greenwich). */
function tzOffset(t: number, tz: string): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      .formatToParts(new Date(t)).map(p => [p.type, p.value]),
  )
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second)
  return asUtc - Math.floor(t / 1000) * 1000
}

/** The instant of wall-clock `y-m-d h:mi` in `tz` (a skipped DST hour lands an hour later). */
function wallToInstant(y: number, m: number, d: number, h: number, mi: number, tz: string): number {
  const wall = Date.UTC(y, m - 1, d, h, mi)
  let t = wall - tzOffset(wall, tz)
  t = wall - tzOffset(t, tz)
  return t
}

/** The next scheduled slot strictly after `now`, or null (no valid slot, or an unknown zone). */
export function nextScan(schedule: unknown, tz: unknown, now: Date): Date | null {
  if (!Array.isArray(schedule) || typeof tz !== 'string') return null
  const slots = schedule.flatMap(s => {
    const m = typeof s === 'string' ? s.match(HHMM) : null
    return m ? [[+m[1], +m[2]] as const] : []
  })
  if (!slots.length) return null
  let today: { y: number; m: number; d: number }
  try {
    const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now).map(x => [x.type, x.value]))
    today = { y: +p.year, m: +p.month, d: +p.day }
  } catch {
    return null   // not an IANA zone this runtime knows
  }
  let best: number | null = null
  for (let day = 0; day <= 1; day++) {
    const base = new Date(Date.UTC(today.y, today.m - 1, today.d + day))
    for (const [h, mi] of slots) {
      const t = wallToInstant(base.getUTCFullYear(), base.getUTCMonth() + 1, base.getUTCDate(), h, mi, tz)
      if (t > now.getTime() && (best == null || t < best)) best = t
    }
  }
  return best == null ? null : new Date(best)
}
