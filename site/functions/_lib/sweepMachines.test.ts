import { describe, expect, it } from 'vitest'
import { BIG_BUCKET_OBJECTS, machineFor, SWEEP_MACHINES } from './sweepMachines'

describe('machineFor — the executor machine for a run, from its biggest bucket\'s planned objects', () => {
  it('the default up to the threshold, the big machine past it (2026-10-03: 170M keys OOM-killed 64 GB)', () => {
    expect([0, 3_863_869, BIG_BUCKET_OBJECTS, BIG_BUCKET_OBJECTS + 1, 170_771_897].map(machineFor)).toEqual([
      'n2-highmem-8', 'n2-highmem-8', 'n2-highmem-8', 'n2-highmem-32', 'n2-highmem-32',
    ])
  })
  it('each machine\'s task resources fit inside it', () => {
    expect(SWEEP_MACHINES).toEqual({
      'n2-highmem-8': { cpuMilli: 8000, memoryMib: 60000 },
      'n2-highmem-32': { cpuMilli: 32000, memoryMib: 250000 },
    })
  })
})
