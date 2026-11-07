/** Meaning of the existing listing age axis, not a conversion of its values.
 * GCS lists timeCreated; S3/R2 and local captures use last-modified time.
 * A future schema carrying both instants must select its axis explicitly. */
export function listingTime(scheme: string) {
  return scheme === 'gs://' || scheme === 'gcs://'
    ? { label: 'created', noun: 'creation', description: 'creation time' } as const
    : { label: 'modified', noun: 'modification', description: 'last-modified time' } as const
}
