// Import-free (the client sizes a dispatch with it too).

/** The machines a gcs executor job may run on. `sweep execute` holds a
 * bucket's whole manifest as Arrow (~230 B/key: 35M keys ≈ 8 GB), so a bucket
 * with ~170M planned keys (2026-10-03, `crawl/`) was OOM-killed on 64 GB. */
export const SWEEP_MACHINES = {
  'n2-highmem-8': { cpuMilli: 8000, memoryMib: 60000 },
  'n2-highmem-32': { cpuMilli: 32000, memoryMib: 250000 },
} as const
export type SweepMachine = keyof typeof SWEEP_MACHINES
export const DEFAULT_SWEEP_MACHINE: SweepMachine = 'n2-highmem-8'
/** Planned objects in one bucket above which a run takes the big machine. */
export const BIG_BUCKET_OBJECTS = 40_000_000
export const machineFor = (maxBucketObjects: number): SweepMachine =>
  maxBucketObjects > BIG_BUCKET_OBJECTS ? 'n2-highmem-32' : DEFAULT_SWEEP_MACHINE
