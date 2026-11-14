//! The shard layer (specs/vm-mode.md §3.1): a shard answers a view over its own range of a pack's object rows with a
//! partial answer, which the merge layer (`merge.rs`) sums by path and cuts. The partial answer is plain data (JSON),
//! so a shard may run in the merging process (`slim_scan::run_sharded`) or behind HTTP (`vmd --role shard`).
//!
//! A shard is logical (a row range of a mapped pack) or physical (`fsb shard`: a self-contained slim pack holding one
//! row range, with the directories its rows sit under, its own sub-dictionary and its open edges; `slim::Edge`).

use std::cmp::Ordering;

use serde::{Deserialize, Serialize};

/// What a shard may leave out of its partial answer. Never a node split across its edge.
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Floor {
    /// One round: drop a whole node the cut fails against the shard's own total. The view's total is at least the
    /// shard's, so this keeps a superset of what the merge draws.
    Local,
    /// The second of two rounds: the view's total, from a first round of totals.
    Total(u64),
    /// Every node (tests).
    All,
}

/// A node as one shard saw it.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct SNode {
    pub path: String,
    pub depth: u8,
    /// An object path (else a directory).
    pub file: bool,
    pub matched: bool,
    /// Its rows reach past the shard's edge: other shards hold the rest.
    pub partial: bool,
    /// `[bytes, objects]` at the scan (a diff's `from`) and at a diff's `to`.
    pub a: [u64; 2],
    pub b: [u64; 2],
    /// Nonempty children wholly inside the shard.
    pub kids: u64,
}

/// One shard's partial answer.
#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct ShardAnswer {
    /// `[bytes a, objects a, bytes b, objects b]` of the folder's rows in the shard.
    pub total: [u64; 4],
    /// The folder's nonempty children wholly inside the shard.
    pub root_kids: u64,
    /// In path order (`cmp_path`).
    pub nodes: Vec<SNode>,
}

/// Paths compared segment by segment (so a directory sorts directly before its subtree).
pub fn cmp_path(a: &str, b: &str) -> Ordering {
    a.split('/').cmp(b.split('/'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_order() {
        let mut v = vec!["a/b-c", "a/b/x", "a", "a/b", "b"];
        v.sort_by(|x, y| cmp_path(x, y));
        assert_eq!(v, vec!["a", "a/b", "a/b/x", "a/b-c", "b"]);
    }

    #[test]
    fn floor_json() {
        assert_eq!(serde_json::to_string(&Floor::Total(7)).unwrap(), r#"{"total":7}"#);
        assert_eq!(serde_json::from_str::<Floor>(r#""local""#).unwrap(), Floor::Local);
    }
}
