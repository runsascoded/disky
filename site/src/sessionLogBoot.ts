/** App build identity over the shared, lazy session logger. */
import { bootSessionLog as boot } from '@open-athena/auth/session-log/client'
export { slog } from '@open-athena/auth/session-log/client'
export const BUILD = import.meta.env.VITE_BUILD_SHA ?? ''
export const bootSessionLog = (): Promise<boolean> => boot({ build: BUILD })
