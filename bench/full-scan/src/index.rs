//! The packed index: one row per object version and one structural row per directory, in depth-first path order
//! (each path directly before its subtree; a path's versions together, by `vf`), as columns.

use serde::{Deserialize, Serialize};

use crate::col::Col;
use crate::dict::Dict;
use crate::store::ScanInfo;

/// `flags` bits.
pub const FILE: u8 = 1;
/// Another version of the previous row's path.
pub const CONT: u8 = 2;
/// Bits 2–3: the class (0..3 = classes 1..4) of a simple file.
pub const CLS_SHIFT: u8 = 2;
/// The file's owner or class slices aren't one whole slice each: see `Index::complex`.
pub const COMPLEX: u8 = 16;

/// Rows between ancestor-chain checkpoints.
pub const CK: usize = 4096;

/// A file version whose owner slices or class bytes aren't a single slice (the per-row "runs").
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Complex {
    /// `(owner id, bytes)`, owner 0 = no owner named.
    pub owners: Vec<(u16, u64)>,
    /// Which classes (bit k−1 for class k) hold a slice, and each class's bytes.
    pub cls_mask: u8,
    pub cls: [u64; 4],
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct LoadStats {
    pub files: usize,
    pub versions_read: u64,
    pub file_versions_read: u64,
    pub dir_versions_read: u64,
    /// Rows that were another tier's copy of a version (`combineLive`: the smallest `vt` kept).
    pub combined: u64,
    pub nodes: u64,
    pub rows: u64,
    pub file_rows: u64,
    pub dir_rows: u64,
    /// Directory versions kept (for the directory-total view), after combining.
    pub dir_versions: u64,
    pub complex_rows: u64,
    pub names: u64,
    pub owners: u64,
    pub max_depth: u8,
    pub secs: Vec<(String, f64)>,
    pub peak_rss_gb: f64,
}

pub struct Index {
    pub scans: Vec<ScanInfo>,
    pub name: Col<u32>,
    pub depth: Col<u8>,
    /// The last row of the path's subtree (inclusive), on every row of the path.
    pub end: Col<u32>,
    pub flags: Col<u8>,
    /// An object version's bytes. On a directory's row: its first version's index in `dir_*` (`owner`: their count).
    pub size: Col<u64>,
    /// Scan ordinals: live at scan `s` iff `vf ≤ s < vt`; `vt` = `scans.len()` while open.
    pub vf: Col<u16>,
    pub vt: Col<u16>,
    pub owner: Col<u16>,
    /// Directory versions (rolled-up totals), each directory's together by `vf`: what the directory-total view reads.
    pub dir_vf: Col<u16>,
    pub dir_vt: Col<u16>,
    pub dir_size: Col<u64>,
    pub dir_files: Col<u64>,
    pub complex_rows: Col<u32>,
    pub complex: Vec<Complex>,
    /// Owner names by id; id 0 is `""`, no owner named.
    pub owners: Vec<String>,
    pub dict: Dict,
    /// Checkpoint `c / CK`'s ancestor chain (group starts before row `c` whose subtree holds it), by depth.
    pub ck_off: Col<u64>,
    pub ck_rows: Col<u32>,
    pub stats: LoadStats,
    /// The pack file the columns map, when opened from one (`pack.rs`).
    pub map: Option<std::sync::Arc<memmap2::Mmap>>,
}

#[derive(Serialize)]
pub struct ColumnBytes {
    pub columns: Vec<(String, u64)>,
    pub total: u64,
    pub per_row: f64,
    pub per_file_version: f64,
}

impl Index {
    pub fn n(&self) -> usize {
        self.depth.len()
    }

    pub fn column_bytes(&self) -> ColumnBytes {
        let n = self.n() as u64;
        let cx: u64 = self.complex.iter().map(|c| 48 + c.owners.len() as u64 * 16).sum::<u64>() + self.complex_rows.len() as u64 * 4;
        let columns = vec![
            ("name".to_string(), n * 4),
            ("depth".to_string(), n),
            ("end".to_string(), n * 4),
            ("flags".to_string(), n),
            ("size".to_string(), n * 8),
            ("vf".to_string(), n * 2),
            ("vt".to_string(), n * 2),
            ("owner".to_string(), n * 2),
            ("complex".to_string(), cx),
            ("dict".to_string(), self.dict.bytes()),
            ("dir versions".to_string(), self.dir_vf.len() as u64 * 20),
            ("checkpoints".to_string(), (self.ck_off.len() * 8 + self.ck_rows.len() * 4) as u64),
        ];
        let total = columns.iter().map(|c| c.1).sum();
        ColumnBytes {
            columns,
            total,
            per_row: total as f64 / n.max(1) as f64,
            per_file_version: total as f64 / self.stats.file_rows.max(1) as f64,
        }
    }

    pub fn scan(&self, id: &str) -> Option<u16> {
        self.scans.iter().position(|s| s.id == id).map(|i| i as u16)
    }

    /// The rows of `g`'s path (its versions): `[g, group_end)`.
    pub fn group_end(&self, g: usize) -> usize {
        let mut r = g + 1;
        while r < self.n() && self.flags[r] & CONT != 0 {
            r += 1;
        }
        r
    }

    /// Group starts before row `r` whose subtree holds it, shallowest first.
    pub fn chain(&self, r: usize) -> Vec<u32> {
        let c = r / CK;
        let mut st: Vec<u32> = self.ck_rows[self.ck_off[c] as usize..self.ck_off[c + 1] as usize].to_vec();
        for x in c * CK..r {
            while let Some(&g) = st.last() {
                if (self.end[g as usize] as usize) < x {
                    st.pop();
                } else {
                    break;
                }
            }
            if self.flags[x] & CONT == 0 {
                st.push(x as u32);
            }
        }
        while let Some(&g) = st.last() {
            if (self.end[g as usize] as usize) < r {
                st.pop();
            } else {
                break;
            }
        }
        st
    }

    /// The full path of group start `g`.
    pub fn path_of(&self, g: usize) -> String {
        let mut parts: Vec<&str> = self.chain(g).iter().map(|&a| self.dict.orig(self.name[a as usize])).collect();
        parts.push(self.dict.orig(self.name[g]));
        parts.join("/")
    }

    /// The group start of `path` (`""` is the virtual root: None), by descending through each level's siblings.
    pub fn find(&self, path: &str) -> Option<usize> {
        if path.is_empty() {
            return None;
        }
        let (mut lo, mut hi) = (0usize, self.n());
        let mut found = None;
        for seg in path.split('/') {
            let id = self.dict.find(seg)?;
            let mut r = lo;
            found = None;
            while r < hi {
                if self.name[r] == id {
                    found = Some(r);
                    break;
                }
                r = self.end[r] as usize + 1;
            }
            let g = found?;
            lo = self.group_end(g);
            hi = self.end[g] as usize + 1;
        }
        found
    }

    /// A path's `(bytes, files)` at scan `s` from its own rows: a live directory version's totals, else a live object
    /// version's bytes and 1; None when nothing of it is live.
    pub fn row_total(&self, g: usize, s: u16) -> Option<(u64, u64)> {
        let mut r = g;
        if self.flags[g] & FILE == 0 {
            let (o, k) = (self.size[g] as usize, self.owner[g] as usize);
            for i in o..o + k {
                if self.dir_vf[i] <= s && s < self.dir_vt[i] {
                    return Some((self.dir_size[i], self.dir_files[i]));
                }
            }
            r += 1;
        }
        let e = self.group_end(g);
        (r..e).find(|&x| self.flags[x] & FILE != 0 && self.vf[x] <= s && s < self.vt[x]).map(|x| (self.size[x], 1))
    }

    pub fn complex_of(&self, r: usize) -> &Complex {
        let i = self.complex_rows.binary_search(&(r as u32)).expect("a COMPLEX row without its slices");
        &self.complex[i]
    }
}
