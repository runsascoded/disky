//! The name dictionary: every distinct path segment once, ids in byte order of the original names. Search runs over
//! one lowercase buffer, `\n` + each lowercase name + `\n`, so a term is one `memmem` over the buffer (anchors are the
//! separators: `^q` = `\nq`, `q$` = `q\n`), giving a bitset of matching ids. Names whose original differs from their
//! lowercase form keep the original in a side buffer (`patch_*`), so paths come back in their own case.

use std::cmp::Ordering;

use rayon::prelude::*;

use crate::col::Col;

/// A predicate on one (lowercase) name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Pat {
    Contains(String),
    Starts(String),
    Ends(String),
    Equals(String),
}

pub struct Dict {
    pub lower: Col<u8>,
    /// `n + 1` offsets: entry `i` is `lower[offs[i] .. offs[i + 1] - 1]`.
    pub offs: Col<u64>,
    pub patch_ids: Col<u32>,
    pub patch_offs: Col<u64>,
    pub patch_buf: Col<u8>,
}

#[derive(Clone)]
pub struct Bitset(pub Vec<u64>);

impl Bitset {
    pub fn new(n: usize) -> Bitset {
        Bitset(vec![0; n.div_ceil(64)])
    }
    pub fn full(n: usize) -> Bitset {
        let mut b = Bitset(vec![!0; n.div_ceil(64)]);
        if n % 64 != 0 {
            *b.0.last_mut().unwrap() = (1u64 << (n % 64)) - 1;
        }
        b
    }
    #[inline(always)]
    pub fn get(&self, i: u32) -> bool {
        unsafe { (*self.0.get_unchecked((i >> 6) as usize) >> (i & 63)) & 1 != 0 }
    }
    pub fn set(&mut self, i: u32) {
        self.0[(i >> 6) as usize] |= 1 << (i & 63);
    }
    pub fn count(&self) -> u64 {
        self.0.iter().map(|w| w.count_ones() as u64).sum()
    }
}

/// `a + "/"` against `b + "/"` in byte order: siblings in depth-first order, each directly before its subtree.
pub fn cmp_slash(a: &[u8], b: &[u8]) -> Ordering {
    let n = a.len().min(b.len());
    match a[..n].cmp(&b[..n]) {
        Ordering::Equal => {}
        o => return o,
    }
    match (a.len() > n, b.len() > n) {
        (false, false) => Ordering::Equal,
        (true, false) => a[n].cmp(&b'/'),
        (false, true) => b'/'.cmp(&b[n]),
        (true, true) => unreachable!(),
    }
}

/// A name's lowercase form in the buffer: Unicode lowercase, a `\n` (which no GCS name holds) as a space.
pub fn lower_of(name: &str) -> String {
    name.to_lowercase().replace('\n', " ")
}

impl Dict {
    /// From the distinct names, sorted by their bytes.
    pub fn build(names: &[Box<str>]) -> Dict {
        Dict::build_iter(names.iter().map(|n| &**n), names.len())
    }

    /// From `n` distinct names, in byte order.
    pub fn build_iter<'a>(names: impl Iterator<Item = &'a str>, n: usize) -> Dict {
        let mut lower = Vec::new();
        let mut offs = Vec::with_capacity(n + 1);
        let (mut patch_ids, mut patch_offs, mut patch_buf) = (Vec::new(), vec![0u64], Vec::new());
        lower.push(b'\n');
        for (i, n) in names.enumerate() {
            offs.push(lower.len() as u64);
            let l = lower_of(n);
            lower.extend_from_slice(l.as_bytes());
            lower.push(b'\n');
            if l.as_str() != n {
                patch_ids.push(i as u32);
                patch_buf.extend_from_slice(n.as_bytes());
                patch_offs.push(patch_buf.len() as u64);
            }
        }
        offs.push(lower.len() as u64);
        Dict { lower: lower.into(), offs: offs.into(), patch_ids: patch_ids.into(), patch_offs: patch_offs.into(), patch_buf: patch_buf.into() }
    }

    pub fn len(&self) -> usize {
        self.offs.len() - 1
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn bytes(&self) -> u64 {
        (self.lower.len() + self.offs.len() * 8 + self.patch_ids.len() * 4 + self.patch_offs.len() * 8 + self.patch_buf.len()) as u64
    }

    pub fn lower(&self, i: u32) -> &[u8] {
        &self.lower[self.offs[i as usize] as usize..self.offs[i as usize + 1] as usize - 1]
    }

    pub fn orig(&self, i: u32) -> &str {
        match self.patch_ids.binary_search(&i) {
            Ok(j) => std::str::from_utf8(&self.patch_buf[self.patch_offs[j] as usize..self.patch_offs[j + 1] as usize]).unwrap(),
            Err(_) => std::str::from_utf8(self.lower(i)).unwrap(),
        }
    }

    /// The id of an original name.
    pub fn find(&self, name: &str) -> Option<u32> {
        let (mut lo, mut hi) = (0u32, self.len() as u32);
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            match self.orig(mid).as_bytes().cmp(name.as_bytes()) {
                Ordering::Less => lo = mid + 1,
                Ordering::Greater => hi = mid,
                Ordering::Equal => return Some(mid),
            }
        }
        None
    }

    /// Entry ranges for `parts` parallel pieces.
    fn pieces(&self, parts: usize) -> Vec<(usize, usize)> {
        let n = self.len();
        let step = n.div_ceil(parts.max(1)).max(1 << 16);
        (0..n).step_by(step).map(|a| (a, (a + step).min(n))).collect()
    }

    /// The ids whose lowercase name satisfies `pat` (the term already lowercase).
    pub fn bitset(&self, pat: &Pat, parts: usize) -> Bitset {
        let n = self.len();
        let (needle, lead, exact) = match pat {
            Pat::Contains(t) => (t.clone(), false, None),
            Pat::Ends(t) => (format!("{t}\n"), false, None),
            Pat::Starts(t) => (format!("\n{t}"), true, None),
            Pat::Equals(t) => (format!("\n{t}"), true, Some(t.len())),
        };
        let term = match pat {
            Pat::Contains(t) | Pat::Ends(t) | Pat::Starts(t) | Pat::Equals(t) => t,
        };
        if term.contains('\n') {
            return Bitset::new(n);
        }
        if term.is_empty() {
            if exact.is_none() {
                return Bitset::full(n);
            }
            let mut bs = Bitset::new(n);
            (0..n as u32).filter(|&i| self.lower(i).is_empty()).for_each(|i| bs.set(i));
            return bs;
        }
        let finder = memchr::memmem::Finder::new(needle.as_bytes());
        let ids: Vec<Vec<u32>> = self
            .pieces(parts * 4)
            .into_par_iter()
            .map(|(a, b)| {
                let base = self.offs[a] as usize - 1;
                let hay = &self.lower[base..self.offs[b] as usize];
                let mut out = Vec::new();
                let mut cur = a;
                for m in finder.find_iter(hay) {
                    let p = (base + m + lead as usize) as u64;
                    // The entry holding `p`: offsets only grow, so advance from the last hit.
                    while cur + 1 < b && self.offs[cur + 1] <= p {
                        cur = if self.offs[(cur + 64).min(b)] <= p { (cur + 64).min(b) } else { cur + 1 };
                    }
                    if let Some(len) = exact {
                        if (self.offs[cur + 1] - self.offs[cur] - 1) as usize != len {
                            continue;
                        }
                    }
                    if out.last() != Some(&(cur as u32)) {
                        out.push(cur as u32);
                    }
                }
                out
            })
            .collect();
        let mut bs = Bitset::new(n);
        for v in ids {
            for i in v {
                bs.set(i);
            }
        }
        bs
    }

    /// The ids whose lowercase name a regex matches (unanchored search, as `regexp_matches`).
    pub fn regex_bitset(&self, re: &regex::bytes::Regex, parts: usize) -> Bitset {
        let ids: Vec<Vec<u32>> = self
            .pieces(parts * 4)
            .into_par_iter()
            .map(|(a, b)| {
                // A clone per piece: its own match cache, no contention on the shared one's pool.
                let re = re.clone();
                (a..b).filter(|&i| re.is_match(self.lower(i as u32))).map(|i| i as u32).collect()
            })
            .collect();
        let mut bs = Bitset::new(self.len());
        for v in ids {
            for i in v {
                bs.set(i);
            }
        }
        bs
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dict() -> Dict {
        let mut names: Vec<Box<str>> = ["a", "ab", "zarr.json", "Zarr.JSON", "x.npy", "abab", "b", "ba"].iter().map(|s| Box::from(*s)).collect();
        names.sort();
        Dict::build(&names)
    }

    fn ids<'a>(d: &'a Dict, b: &Bitset) -> Vec<&'a str> {
        (0..d.len() as u32).filter(|&i| b.get(i)).map(|i| d.orig(i)).collect()
    }

    #[test]
    fn patterns() {
        let d = dict();
        assert_eq!(ids(&d, &d.bitset(&Pat::Contains("ab".into()), 2)), vec!["ab", "abab"]);
        assert_eq!(ids(&d, &d.bitset(&Pat::Contains("zarr.json".into()), 2)), vec!["Zarr.JSON", "zarr.json"]);
        assert_eq!(ids(&d, &d.bitset(&Pat::Starts("b".into()), 2)), vec!["b", "ba"]);
        assert_eq!(ids(&d, &d.bitset(&Pat::Ends("b".into()), 2)), vec!["ab", "abab", "b"]);
        assert_eq!(ids(&d, &d.bitset(&Pat::Equals("b".into()), 2)), vec!["b"]);
        assert_eq!(ids(&d, &d.bitset(&Pat::Equals("a".into()), 2)), vec!["a"]);
        assert_eq!(ids(&d, &d.bitset(&Pat::Contains("".into()), 2)).len(), 8);
        let re = regex::bytes::Regex::new(r"^a(b)?$").unwrap();
        assert_eq!(ids(&d, &d.regex_bitset(&re, 2)), vec!["a", "ab"]);
        assert_eq!(d.find("Zarr.JSON").map(|i| d.orig(i)), Some("Zarr.JSON"));
        assert_eq!(d.find("nope"), None);
    }

    #[test]
    fn slash_order() {
        let mut v = vec!["a", "a-b", "a.c", "ab", "a0"];
        v.sort_by(|x, y| cmp_slash(x.as_bytes(), y.as_bytes()));
        assert_eq!(v, vec!["a-b", "a.c", "a", "a0", "ab"]);
    }
}
