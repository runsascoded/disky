/** Shared wire contract and pure event presentation. */
export * from '@open-athena/auth/session-log'
import type { SlogEvent } from '@open-athena/auth/session-log'
import { summarize as sharedSummary } from '@open-athena/auth/session-log/model'

export const summarize = (event: SlogEvent): string => sharedSummary({ ...event, pid: '', seq: 0 })
