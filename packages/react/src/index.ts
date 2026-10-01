// The treemap core + primitives now live in `@rdub/treemap`. This package
// re-exports them so existing `@disk-tree/react` consumers keep working
// unchanged; new/non-disk consumers should depend on `@rdub/treemap` directly.
export * from '@rdub/treemap'

// disk-flavored widgets built on the core.
export { pow10 } from './stats'
export { BytesOverTime, TimeSeries } from './TimeSeries'
export type { Annotation, Series, TimeSeriesProps } from './TimeSeries'
