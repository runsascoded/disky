//! Design A of `specs/full-scan-serving-bench.md`: an interval store held in RAM in depth-first path order, every
//! view (scan or diff, folder, search, owner, class, staged) answered by one bounded pass.

pub mod cell;
pub mod col;
pub mod dict;
pub mod index;
pub mod load;
pub mod merge;
pub mod pack;
pub mod plan;
pub mod query;
pub mod scan;
pub mod serve;
pub mod shard;
pub mod slim;
pub mod slim_scan;
pub mod store;
pub mod synth;
pub mod util;
