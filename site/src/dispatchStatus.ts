import { HttpError } from './batches'

export function dispatchFailure(error: Error): string {
  if (error instanceof HttpError && error.details && typeof error.details === 'object' && 'code' in error.details && error.details.code === 'cleanup_agent_offline') return error.message
  if (error instanceof HttpError && error.status < 500) return error.message
  return `Submission could not be confirmed. Check Runs before retrying: the request may have reached the executor. ${error.message}`
}
