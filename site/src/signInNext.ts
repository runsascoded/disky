import { safeNext } from '@open-athena/auth'

/** `/signin?next=<path>` → where to go after signing in: a path on this origin, as a browser would read it, or home. */
export const signInNext = (search: string): string => safeNext(new URLSearchParams(search).get('next'), '/')
