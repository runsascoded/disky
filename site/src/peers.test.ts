import { describe, expect, it } from 'vitest'
import { parsePeers } from './peers'

describe('parsePeers', () => {
  it('unset or blank: no peers', () => {
    expect(parsePeers(undefined)).toEqual([])
    expect(parsePeers('')).toEqual([])
    expect(parsePeers('  ')).toEqual([])
  })

  it('reads each peer, keeping only the fields given', () => {
    const raw = JSON.stringify([
      { label: 'A usage', prod: 'a.example.com', dev: 'dev.a.example.com', owners: true, staging: true, meta: true },
      { label: 'B usage', prod: 'b.example.com', meta: false },
      { label: 'C usage', prod: 'c.example.com' },
    ])
    expect(parsePeers(raw)).toEqual([
      { label: 'A usage', prod: 'a.example.com', dev: 'dev.a.example.com', owners: true, staging: true, meta: true },
      { label: 'B usage', prod: 'b.example.com', meta: false },
      { label: 'C usage', prod: 'c.example.com' },
    ])
  })

  it('refuses malformed config, naming the entry', () => {
    const msg = (raw: string) => {
      try {
        parsePeers(raw)
      } catch (e) {
        return (e as Error).message
      }
      return null
    }
    expect([
      msg('{"label": "A"}'),
      msg('[1]'),
      msg('[{"label": "A"}]'),
      msg('[{"label": "A", "prod": ""}]'),
      msg('[{"label": "A", "prod": "a", "dev": 3}]'),
      msg('[{"label": "A", "prod": "a", "owners": "yes"}]'),
      msg('[{"label": "A", "prod": "a", "host": "a", "extra": 1}]'),
    ]).toEqual([
      'PEERS: expected a JSON array of peers',
      'PEERS[0]: expected an object',
      'PEERS[0].prod: expected a non-empty string',
      'PEERS[0].prod: expected a non-empty string',
      'PEERS[0].dev: expected a non-empty string',
      'PEERS[0].owners: expected a boolean',
      'PEERS[0]: unknown keys extra, host',
    ])
    // the parser's own message follows, and it varies by JS engine: only the prefix is ours
    expect(msg('[{')?.startsWith('PEERS: not JSON (')).toBe(true)
  })
})
