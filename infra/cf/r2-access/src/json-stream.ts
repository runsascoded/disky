type RowsDocument = { rows: readonly unknown[] }

/** The aggregate's rows are last on disk. Emit the same JSON bytes without materializing the whole document. */
export function* jsonChunks(value: RowsDocument): Generator<Uint8Array> {
  const { rows, ...fields } = value
  const encoder = new TextEncoder()
  let chunk = JSON.stringify(fields).slice(0, -1) + (Object.keys(fields).length ? ',' : '') + '"rows":['
  for (let i = 0; i < rows.length; i++) {
    chunk += (i ? ',' : '') + (JSON.stringify(rows[i]) ?? 'null')
    if (chunk.length >= 65_536) { yield encoder.encode(chunk); chunk = '' }
  }
  yield encoder.encode(chunk + ']}')
}

export async function hashJson(value: RowsDocument): Promise<{ digest: string, bytes: number }> {
  const stream = new crypto.DigestStream('SHA-256')
  const writer = stream.getWriter()
  let bytes = 0
  for (const chunk of jsonChunks(value)) { bytes += chunk.byteLength; await writer.write(chunk) }
  await writer.close()
  const digest = [...new Uint8Array(await stream.digest)].map(b => b.toString(16).padStart(2, '0')).join('')
  return { digest, bytes }
}

export async function putJson(archive: Pick<R2Bucket, 'put'>, key: string, value: RowsDocument, bytes: number): Promise<void> {
  const stream = new FixedLengthStream(bytes)
  const chunks = jsonChunks(value)
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = chunks.next()
      if (next.done) controller.close()
      else controller.enqueue(next.value)
    },
  })
  const abort = new AbortController()
  try {
    await Promise.all([
      body.pipeTo(stream.writable, { signal: abort.signal }),
      archive.put(key, stream.readable, { httpMetadata: { contentType: 'application/json' } }),
    ])
  } catch (error) {
    abort.abort(error)
    throw error
  }
}
