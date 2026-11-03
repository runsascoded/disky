import { expect, it } from 'vitest'
import { hashJson, jsonChunks, putJson } from './json-stream'

it.each([
  { rows: [] },
  { schema_version: 4, bucket: 'é/😀', rows: [[null, 'HeadObject', 1], ['a"\\\n', 'GetObject', 2]] },
  { rows: Array.from({ length: 2000 }, (_, i) => [`key-${i}-${'é'.repeat(100)}`, 'HeadObject', i]) },
])('preserves the exact JSON bytes and SHA-256 for streamed rows', async value => {
  const expected = JSON.stringify(value)
  const chunks = [...jsonChunks(value)]
  const body = new Blob(chunks)
  expect(await body.text()).toBe(expected)
  const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(expected)))]
    .map(b => b.toString(16).padStart(2, '0')).join('')
  expect(await hashJson(value)).toEqual({ digest, bytes: body.size })
})

it('uploads the same JSON with its measured byte length', async () => {
  const value = { rows: [['a', 'HeadObject', 1]] }
  const { bytes } = await hashJson(value)
  const archive = { put: async (key: string, body: ReadableStream, options: R2PutOptions) => {
    expect({ key, options, text: await new Response(body).text() }).toEqual({
      key: 'agg.json', options: { httpMetadata: { contentType: 'application/json' } }, text: '{"rows":[["a","HeadObject",1]]}',
    })
    return null
  } }
  await putJson(archive as Pick<R2Bucket, 'put'>, 'agg.json', value, bytes)
})

it('propagates a rejected upload and aborts the producer', async () => {
  const value = { rows: [['a', 'HeadObject', 1]] }
  const archive = { put: async () => { throw new Error('storage unavailable') } }
  await expect(putJson(archive, 'agg.json', value, (await hashJson(value)).bytes)).rejects.toThrow('storage unavailable')
})
