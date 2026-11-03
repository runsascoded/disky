const assert = require('node:assert/strict')
require('./worker-streams.cjs')
const { HourAccumulator } = require('./index.js')
const { hashJson, putJson } = require('./json-stream.js')

const count = 700_000
const start = Date.parse('2026-10-19T18:00:00Z')
const alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
const acc = new HourAccumulator(count)
for (let i = 0; i < count; i++) {
  let n = i
  let suffix = ''
  for (let j = 0; j < 6; j++) { suffix = alphabet[n % 32] + suffix; n = Math.floor(n / 32) }
  const id = '01K7F000000000000000' + suffix
  const read = i % 5 < 2
  const key = read ? `data/${String(Math.floor(i / 5)).padStart(8, '0')}/${'x'.repeat(270)}` : null
  const event = { id, timestamp: start + i, action: read ? 'HeadObject' : 'ListObjectsV2', key, own: false }
  acc.add(event)
  if (i % 1000 === 0) acc.add(event)
}
const result = acc.finish('marin-na', start, count, 1)
assert.equal(result.events, count)
assert.equal(result.rows.length, 140_000)
assert.deepEqual(result.actions, { HeadObject: 280_000, ListObjectsV2: 420_000 })
assert.equal(result.rows.every(row => row[2] === 2), true)
hashJson(result).then(async ({ bytes }) => {
  let written = 0
  await putJson({ put: async (_key, stream) => {
    const reader = stream.getReader()
    for (;;) { const chunk = await reader.read(); if (chunk.done) break; written += chunk.value.byteLength }
  } }, 'aggregate.json', result, bytes)
  assert.equal(written, bytes)
  process.stdout.write(JSON.stringify({ events: result.events, rows: result.rows.length, actions: result.actions }) + '\n')
})
