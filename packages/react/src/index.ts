// The treemap core + primitives now live in `@open-athena/treemap`. This package
// re-exports them so existing `@disk-tree/react` consumers keep working
// unchanged; new/non-disk consumers should depend on `@open-athena/treemap` directly.
export * from '@open-athena/treemap'

// disk-flavored widgets built on the core.
export { pow10 } from './stats'
export { BytesOverTime, gapStretches, MARKER_LANE, placeMarkers, splitAtGaps, TimeSeries } from './TimeSeries'
export type { Annotation, Marker, PlacedMarker, Series, TimeSeriesProps } from './TimeSeries'
