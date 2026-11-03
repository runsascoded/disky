const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
const DIGITS = new Uint8Array(128).fill(255)
for (let i = 0; i < ALPHABET.length; i++) DIGITS[ALPHABET.charCodeAt(i)] = i
type Words = [number, number, number, number]

/** Canonical ULIDs carry 128 bits in 26 base-32 characters (the first digit has only three bits). */
function unpack(id: string): Words | null {
  if (id.length !== 26 || id.charCodeAt(0) > 55 || id.charCodeAt(0) < 48) return null
  const words: number[] = []
  let value = id.charCodeAt(0) - 48
  let bits = 3
  for (let i = 1; i < id.length; i++) {
    const digit = DIGITS[id.charCodeAt(i)]
    if (digit === undefined || digit === 255) return null
    value = value * 32 + digit
    bits += 5
    if (bits >= 32) {
      const remainder = 2 ** (bits - 32)
      words.push(Math.floor(value / remainder) >>> 0)
      value %= remainder
      bits -= 32
    }
  }
  return words as Words
}

/** Exact deduplication: ULIDs occupy four uint32 words per table slot, without retaining their JS strings.
 * Hash collisions compare all 128 bits. Other provider ID formats retain ordinary string-set semantics. */
export class EventIds {
  private words: Uint32Array
  private used: Uint8Array
  private count = 0
  private readonly other = new Set<string>()

  constructor(expected = 0) {
    let capacity = 16
    while (capacity * 0.75 < expected) capacity *= 2
    this.words = new Uint32Array(capacity * 4)
    this.used = new Uint8Array(capacity)
  }

  get size(): number { return this.count + this.other.size }

  private slot(words: Words): number {
    let hash = (words[0] ^ words[1] ^ words[2] ^ words[3]) >>> 0
    hash = Math.imul(hash ^ (hash >>> 16), 0x85ebca6b)
    hash = Math.imul(hash ^ (hash >>> 13), 0xc2b2ae35)
    hash ^= hash >>> 16
    const mask = this.used.length - 1
    let slot = hash & mask
    while (this.used[slot]) {
      const offset = slot * 4
      if (words.every((word, i) => this.words[offset + i] === word)) break
      slot = (slot + 1) & mask
    }
    return slot
  }

  private insert(words: Words, slot: number): void {
    this.words.set(words, slot * 4)
    this.used[slot] = 1
    this.count++
  }

  private grow(): void {
    const oldWords = this.words
    const oldUsed = this.used
    this.words = new Uint32Array(oldWords.length * 2)
    this.used = new Uint8Array(oldUsed.length * 2)
    this.count = 0
    for (let slot = 0; slot < oldUsed.length; slot++) {
      if (!oldUsed[slot]) continue
      const offset = slot * 4
      const words: Words = [oldWords[offset], oldWords[offset + 1], oldWords[offset + 2], oldWords[offset + 3]]
      this.insert(words, this.slot(words))
    }
  }

  add(id: string): boolean {
    const words = unpack(id)
    if (!words) {
      if (this.other.has(id)) return false
      this.other.add(id)
      return true
    }
    let slot = this.slot(words)
    if (this.used[slot]) return false
    if ((this.count + 1) > this.used.length * 0.75) { this.grow(); slot = this.slot(words) }
    this.insert(words, slot)
    return true
  }

  has(id: string): boolean {
    const words = unpack(id)
    return words ? Boolean(this.used[this.slot(words)]) : this.other.has(id)
  }
}
