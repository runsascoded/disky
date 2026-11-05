/** The scan `/staged` sizes at: the one picked, while the list still has it, else the newest (`scans` newest first). */
export const sizedScan = (scans: readonly string[], picked: string | null): string =>
  picked && scans.includes(picked) ? picked : scans[0] ?? ''
