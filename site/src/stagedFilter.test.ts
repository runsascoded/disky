import { describe, expect, it } from 'vitest'
import { encodeSort, filterStaged, parseSort, stagedHaystack, type StagedFields } from './stagedFilter'

const NAMES: Record<string, string> = {
  'hedy-lamarr': 'Hedy',
  'grace-hopper': 'Grace',
  'alan-turing': 'Alan',
  'ken.alanson@example.org': 'Ken',
  'barbara.liskov@example.org': 'Barbara',
}
const name = (who: string) => NAMES[who] ?? who

const ROWS: StagedFields[] = [
  { prefix: 'gs://marin-us-central2/checkpoints/sft/mixture_sft_deeper_starling-de8fa2/', owners: ['grace-hopper'], stagedBy: 'ken.alanson@example.org' },
  { prefix: 'gs://marin-us-central2/checkpoints/sft/tulu3_sft_spoonbill_945_lr_1e-04/', owners: ['hedy-lamarr'], stagedBy: 'ken.alanson@example.org' },
  { prefix: 'gs://marin-us-central2/checkpoints/ferry_qwen3_8b_pt_to_cooldown-1fc2cb/', owners: ['alan-turing'], stagedBy: 'ken.alanson@example.org' },
  { prefix: 'gs://marin-eu-west4/ego-dex/', owners: [], stagedBy: 'ken.alanson@example.org' },
  { prefix: 'gs://marin-us-central1/grug/tied_experts/d512/', owners: ['barbara.liskov@example.org'], stagedBy: 'barbara.liskov@example.org' },
  { prefix: 'gs://marin-us-central1/checkpoints/isoflop/isoflop-1e+19-d1024-nemotron/', owners: ['hedy-lamarr', 'alan-turing'], stagedBy: 'ken.alanson@example.org' },
]
const kept = (q: string) => {
  const r = filterStaged(ROWS, q, x => x, name)
  return r.error ? { error: r.error } : r.rows.map(x => x.prefix.replace(/^gs:\/\/marin-/, ''))
}

describe('filterStaged: the map syntax over prefix, owners and stager', () => {
  it('owners by display name or id: `hedy|grace`', () => {
    expect(kept('hedy|grace')).toEqual([
      'us-central2/checkpoints/sft/mixture_sft_deeper_starling-de8fa2/',
      'us-central2/checkpoints/sft/tulu3_sft_spoonbill_945_lr_1e-04/',
      'us-central1/checkpoints/isoflop/isoflop-1e+19-d1024-nemotron/',
    ])
  })

  it('AND across fields, NOT, globs, and the stager', () => {
    expect([kept('owner:alan checkpoints -isoflop'), kept('alan'), kept('sft -hedy'), kept('staged-by:barbara'), kept('ego-*'), kept('')]).toEqual([
      ['us-central2/checkpoints/ferry_qwen3_8b_pt_to_cooldown-1fc2cb/'],
      // A bare `alan` is a substring of `ken.alanson` too: every row Ken staged.
      ROWS.filter(x => x.stagedBy.startsWith('ken')).map(x => x.prefix.replace(/^gs:\/\/marin-/, '')),
      ['us-central2/checkpoints/sft/mixture_sft_deeper_starling-de8fa2/'],
      ['us-central1/grug/tied_experts/d512/'],
      ['eu-west4/ego-dex/'],
      ROWS.map(x => x.prefix.replace(/^gs:\/\/marin-/, '')),
    ])
  })

  it('a term never spans two fields; a too-short term is an error, not a silent empty set', () => {
    expect(stagedHaystack(ROWS[4], name).split('\n')).toEqual([
      'gs://marin-us-central1/grug/tied_experts/d512/',
      'owner:barbara.liskov@example.org Barbara',
      'staged-by:barbara.liskov@example.org Barbara',
    ])
    expect([kept('"d512/ owner"'), kept('"d512/"')]).toEqual([[], ['us-central1/grug/tied_experts/d512/']])
    expect(kept('ab')).toEqual({ error: expect.stringMatching(/3/) })
  })
})

describe('sort in the URL', () => {
  it('`-k` descending (default `-b` omitted), `k` ascending', () => {
    expect([parseSort(undefined), parseSort('-o'), parseSort('name'), encodeSort({ k: 'b', asc: false }), encodeSort({ k: 'staged', asc: true }), encodeSort({ k: 'd', asc: false })])
      .toEqual([{ k: 'b', asc: false }, { k: 'o', asc: false }, { k: 'name', asc: true }, undefined, 'staged', '-d'])
  })
})
