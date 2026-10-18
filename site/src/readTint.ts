import { enumParam, useUrlState } from 'use-prms'
import { READ_TINTS, type ReadTint } from './readLens'

/** `?rt=` — the read lens's tint (`readLens.ts` `ReadTint`): `hatch` (default, omitted from the URL) or
 *  `touch`. Shared by the map, its tables and the age chart; the OG card always draws `hatch`. */
export const readTintParam = enumParam<ReadTint>('hatch', READ_TINTS)

export const useReadTint = () => useUrlState('rt', readTintParam)
