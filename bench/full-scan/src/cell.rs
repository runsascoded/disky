//! The cell rule (specs/vm-mode.md §2.2, specs/serverless-tiers.md §7 item 1): which nodes a view draws.
//!
//! A node `r` levels below the folder (`r ≥ 1`), with bytes `x` (a diff: the larger side's), in a view whose total is
//! `T` (a diff: the larger side's), is drawn when it is nonempty, `r ≤ depth`, and
//!
//! ```text
//! x · 2^share_log2 ≥ T · 2^(atten_log2 · (r − 1))        (share)
//! x ≥ floor_bytes · 2^(atten_log2 · (r − 1))              (an absolute floor, when given)
//! x · 10^6 ≥ min_ppm · T                                  (the bench's legacy form)
//! ```
//!
//! in exact integer arithmetic. Every test is monotone: a larger `T` only drops nodes, and a node never passes where
//! its parent fails (bytes only shrink down a path, the right side only grows), so the drawn set is a tree, and a
//! cut against any `T' ≤ T` keeps a superset of the cut against `T` (what a shard or a chunk that hasn't seen the
//! whole total relies on).

use serde::{Deserialize, Serialize};

use crate::util::{err, Result};

/// The deepest window a request may ask for.
pub const MAX_DEPTH: u8 = 64;

/// The rule as a request carries it.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct Cell {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub share_log2: Option<u8>,
    #[serde(default)]
    pub atten_log2: u8,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub depth: Option<u8>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub floor_bytes: Option<u64>,
}

/// The rule, resolved for one folder.
#[derive(Clone, Copy, Debug)]
pub struct Cut {
    /// The folder's depth.
    pub dp: u8,
    /// The window: nodes at relative depths `1..=depth`.
    pub depth: u8,
    pub share_log2: Option<u8>,
    pub atten_log2: u8,
    pub floor_bytes: u64,
    pub min_ppm: u64,
}

impl Cell {
    /// The window when the request doesn't give one: as deep as a node can pass under attenuation, else 8.
    pub fn default_depth(&self) -> u8 {
        match (self.share_log2, self.atten_log2) {
            (Some(s), a) if a > 0 => (1 + s / a).min(MAX_DEPTH),
            _ => 8,
        }
    }

    pub fn cut(&self, dp: u8) -> Result<Cut> {
        let depth = self.depth.unwrap_or_else(|| self.default_depth());
        if depth == 0 || depth > MAX_DEPTH {
            return err(format!("cell.depth {depth}: 1..={MAX_DEPTH}"));
        }
        if self.share_log2.is_some_and(|s| s > 63) || self.atten_log2 > 63 {
            return err("cell.share_log2 and cell.atten_log2 are at most 63");
        }
        Ok(Cut { dp, depth, share_log2: self.share_log2, atten_log2: self.atten_log2, floor_bytes: self.floor_bytes.unwrap_or(0), min_ppm: 0 })
    }
}

impl Cut {
    /// The bench's rule: `min_ppm` millionths of the total, a fixed window, no attenuation.
    pub fn ppm(dp: u8, depth: u8, min_ppm: u64) -> Cut {
        Cut { dp, depth, share_log2: None, atten_log2: 0, floor_bytes: 0, min_ppm }
    }

    /// The deepest absolute depth drawn.
    pub fn maxd(&self) -> u8 {
        self.dp.saturating_add(self.depth)
    }

    /// Whether a nonempty node at absolute depth `d` with bytes `x` is drawn in a view whose total is `total`.
    #[inline]
    pub fn keep(&self, x: u64, d: u8, total: u64) -> bool {
        if d <= self.dp || d > self.maxd() {
            return false;
        }
        let shift = self.atten_log2 as u32 * (d - self.dp - 1) as u32;
        let x = x as u128;
        if let Some(s) = self.share_log2 {
            // T · 2^shift overflows u128 only past shift 63: then nothing nonzero passes (x · 2^s < 2^127).
            if shift > 63 {
                if total > 0 {
                    return false;
                }
            } else if x << s < (total as u128) << shift {
                return false;
            }
        }
        if self.floor_bytes > 0 && (shift > 63 || x < (self.floor_bytes as u128) << shift) {
            return false;
        }
        if self.min_ppm > 0 && x * 1_000_000 < self.min_ppm as u128 * total as u128 {
            return false;
        }
        true
    }

    /// Whether the rule can drop anything (a chunk tracks the running total only then).
    pub fn prunes(&self) -> bool {
        self.share_log2.is_some() || self.floor_bytes > 0 || self.min_ppm > 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn share_and_atten() {
        // Folder at depth 1, share 2^-4, halving per level: depth 2 needs x ≥ T/16, depth 3 x ≥ T/8.
        let c = Cell { share_log2: Some(4), atten_log2: 1, depth: None, floor_bytes: None }.cut(1).unwrap();
        assert_eq!(c.depth, 5);
        assert_eq!(
            [(100, 2), (99, 2), (200, 3), (199, 3), (1600, 6), (1600, 7)].map(|(x, d)| c.keep(x, d, 1600)),
            [true, false, true, false, true, false]
        );
    }

    #[test]
    fn floor_and_ppm() {
        let c = Cell { share_log2: None, atten_log2: 1, depth: Some(3), floor_bytes: Some(10) }.cut(0).unwrap();
        assert_eq!([(10, 1), (9, 1), (20, 2), (19, 2), (40, 3), (1000, 4)].map(|(x, d)| c.keep(x, d, 0)), [true, false, true, false, true, false]);
        let p = Cut::ppm(0, 2, 500);
        assert_eq!([(500, 1), (499, 1), (500, 3)].map(|(x, d)| p.keep(x, d, 1_000_000)), [true, false, false]);
    }

    #[test]
    fn deep_shift() {
        let c = Cell { share_log2: Some(63), atten_log2: 63, depth: Some(3), floor_bytes: None }.cut(0).unwrap();
        assert_eq!([c.keep(u64::MAX, 1, u64::MAX), c.keep(u64::MAX, 2, u64::MAX), c.keep(1, 3, 0)], [true, true, true]);
        assert!(!c.keep(u64::MAX, 3, 1));
    }
}
