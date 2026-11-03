const { createHash } = require('node:crypto')
const assert = require('node:assert/strict')

class DigestStream extends WritableStream {
  constructor(algorithm) {
    const hash = createHash(algorithm.replaceAll('-', '').toLowerCase())
    let resolve
    const digest = new Promise(done => { resolve = done })
    super({
      write(chunk) { hash.update(chunk) },
      close() { const bytes = hash.digest(); resolve(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)) },
    })
    this.digest = digest
  }
}

class FixedLengthStream extends TransformStream {
  constructor(bytes) {
    let written = 0
    super({
      transform(chunk, controller) { written += chunk.byteLength; assert.ok(written <= bytes); controller.enqueue(chunk) },
      flush() { assert.equal(written, bytes) },
    })
  }
}

Object.defineProperty(crypto, 'DigestStream', { value: DigestStream, configurable: true })
globalThis.FixedLengthStream = FixedLengthStream
