/** Put low-info entries on one line without combining their quotas or discarding their individual text. */
export function compactQuietLines(rows: { text: string; quiet: boolean }[]): string[] {
  const quiet = rows.filter(r => r.quiet)
  if (quiet.length < 2) return rows.map(r => r.text)
  let emitted = false
  return rows.flatMap(r => {
    if (!r.quiet) return [r.text]
    if (emitted) return []
    emitted = true
    return [quiet.map(x => x.text).join(' · ')]
  })
}
