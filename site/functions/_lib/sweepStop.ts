import { NotFoundError, type GetResult } from '@rdub/file-tree'

/** Exit 130 alone is ambiguous; require the executor's persistent STOP marker. */
export async function intentionalStop(job: { job_id: string; state: string; last_event: string | null }, get: (key: string, range: { offset: number; length: number }) => Promise<GetResult>): Promise<boolean> {
  if (job.state !== 'FAILED' || !/exit code 130\b/.test(job.last_event ?? '')) return false
  const key = `sweep/runs/${job.job_id}/STOP`
  try {
    await get(key, { offset: 0, length: 1 })
    return true
  } catch (error) {
    if (error instanceof NotFoundError) return false
    throw error
  }
}
