/** Bounded read-only Logging queries; callers cannot provide arbitrary filters. */
export interface TaskLogEntry { id: string; timestamp: string; severity: string; message: string; truncated: boolean }
export interface TaskLogPage { entries: TaskLogEntry[]; nextPageToken: string | null; until: string }

export function taskLogBody(view: string, uid: string, created: string, until: string, cursor: string | null) {
  return {
    resourceNames: [view],
    filter: `log_id("batch_task_logs")\nlabels.job_uid=${JSON.stringify(uid)}\ntimestamp >= ${JSON.stringify(created)}\ntimestamp <= ${JSON.stringify(until)}`,
    orderBy: 'timestamp desc', pageSize: 50,
    ...(cursor ? { pageToken: cursor } : {}),
  }
}

/** Cloud entries may contain JSON rather than text. Never render either as HTML. */
export function taskLogEntries(value: unknown): TaskLogEntry[] {
  if (value == null) return []
  if (!Array.isArray(value)) throw new Error('invalid Logging entries')
  return (value as unknown[]).map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('invalid Logging entry')
    const entry = item as Record<string, unknown>
    if (typeof entry.timestamp !== 'string') throw new Error('invalid Logging entry')
    const text = typeof entry.textPayload === 'string' ? entry.textPayload : JSON.stringify(entry.jsonPayload ?? entry.protoPayload ?? {})
    return {
      id: typeof entry.insertId === 'string' ? entry.insertId : `${entry.timestamp}:${index}`,
      timestamp: entry.timestamp,
      severity: typeof entry.severity === 'string' ? entry.severity : 'DEFAULT',
      message: text.slice(0, 16_384), truncated: text.length > 16_384,
    }
  })
}

/** Cap upstream bodies before parsing, even if Content-Length is omitted. */
export async function boundedJson(response: Response, limit: number): Promise<unknown> {
  if (!response.body) throw new Error('empty upstream response')
  const reader = response.body.getReader(), chunks: Uint8Array[] = []
  let size = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > limit) { await reader.cancel(); throw new Error('upstream response exceeds size limit') }
      chunks.push(value)
    }
  } finally { reader.releaseLock() }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  return JSON.parse(new TextDecoder().decode(bytes)) as unknown
}
