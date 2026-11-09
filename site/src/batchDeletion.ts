/** Recorded outcomes for the runs that completed a batch's prefixes. Missing bands remain unknown. */
export interface BatchDeletion { bytes: number; objects: number; prefixes: number }

export const addBatchDeletion = (a: BatchDeletion | undefined, b: BatchDeletion | undefined): BatchDeletion | undefined =>
  a && b ? { bytes: a.bytes + b.bytes, objects: a.objects + b.objects, prefixes: a.prefixes + b.prefixes } : a ?? b
