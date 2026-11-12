//! A view's inputs (SEDQUCLM) and their compiled form.
//!
//! Semantics (the oracle, `oracle/oracle.py`, implements the same in SQL):
//! - Bytes come from object versions (`kind = 'file'`) live at the scan (`vf ≤ ts < vt`), under `path`.
//! - **Search** (`q`): a name term (`{"t": …}`, lowercase substring of a path segment; `^q`, `q$`, `^q$` anchor it to
//!   the segment's start / end / whole) matches a file when some segment of its full path matches, so a folder's match
//!   covers its subtree. A path term (`{"p": "a/b"}`) matches when the lowercase path holds it (`^` / `$` anchor its ends
//!   to segment boundaries). A regex (`{"re": …}`) is searched in each lowercase segment. `and` / `or` / `not` combine
//!   them per file.
//! - **Owners**: a file's slices are its `us` (`''` → no owner; a name → that owner, all bytes; JSON → each entry, plus
//!   the unnamed rest when positive). `{"in": [...]}` sums the named owners' slices (`""` = no owner), `{"not_in": …}`
//!   the others'; a file counts when it holds a selected slice.
//! - **Classes**: slices `c2..c4` where positive, class 1 the rest when positive (a file with neither: class 1, 0 B).
//! - Owners and classes together: per file, the class filter gates the owner sum when the file is in one class, else
//!   the owner filter gates the class sum when it has one owner; a file with several of both is an error.
//! - **Staged**: only files at or under one of the prefixes.
//! - The answer: every node at depths `1..=depth` below `path` whose bytes (the larger side's, in a diff) are at least
//!   `min_ppm` millionths of the view's total (the larger side's), nonempty; each node's bytes and files per side; and
//!   per kept node above the deepest level (and `path` itself) the rest its kept children leave, when nonzero.

use serde::{Deserialize, Serialize};

use crate::dict::{lower_of, Bitset, Pat};
use crate::index::Index;
use crate::util::{err, Result};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Expr {
    T(String),
    P(String),
    Re(String),
    And(Vec<Expr>),
    Or(Vec<Expr>),
    Not(Box<Expr>),
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum OwnerSel {
    In(Vec<String>),
    NotIn(Vec<String>),
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Query {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scan: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub from: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub to: Option<String>,
    #[serde(default)]
    pub path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub q: Option<Expr>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owners: Option<OwnerSel>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub classes: Option<Vec<u8>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub staged: Option<Vec<String>>,
    pub depth: u8,
    pub min_ppm: u64,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tags: Vec<String>,
}

pub struct PathAtom {
    pub bit: u32,
    /// Per part, the names satisfying it (first: ends with, or equals when `^`; middle: equals; last: starts with, or
    /// equals when `$`).
    pub parts: Vec<Bitset>,
}

pub struct Search {
    pub name_atoms: Vec<(u32, Bitset)>,
    pub path_atoms: Vec<PathAtom>,
    /// `lut[mask]`: the expression's value for that set of matching atoms.
    pub lut: Vec<bool>,
    /// Dictionary bytes the atoms scanned.
    pub dict_bytes: u64,
}

pub struct Compiled {
    pub sa: u16,
    pub sb: Option<u16>,
    /// `path`'s depth and its subtree's rows (its own versions excluded).
    pub dp: u8,
    pub lo: usize,
    pub hi: usize,
    pub path: String,
    pub maxd: u8,
    pub min_ppm: u64,
    pub search: Option<Search>,
    pub owners: Option<Vec<bool>>,
    /// Bit k−1 for class k.
    pub classes: Option<u8>,
    /// Merged `[first, last]` row spans of the staged prefixes.
    pub staged: Option<Vec<(u32, u32)>>,
}

impl Compiled {
    pub fn filtered(&self) -> bool {
        self.owners.is_some() || self.classes.is_some() || self.staged.is_some()
    }
}

fn leaves<'a>(e: &'a Expr, out: &mut Vec<&'a Expr>) {
    match e {
        Expr::And(xs) | Expr::Or(xs) => xs.iter().for_each(|x| leaves(x, out)),
        Expr::Not(x) => leaves(x, out),
        leaf => out.push(leaf),
    }
}

fn eval(e: &Expr, mask: u32, next: &mut u32) -> bool {
    match e {
        Expr::And(xs) => xs.iter().fold(true, |acc, x| eval(x, mask, next) && acc),
        Expr::Or(xs) => xs.iter().fold(false, |acc, x| eval(x, mask, next) || acc),
        Expr::Not(x) => !eval(x, mask, next),
        _ => {
            let b = mask >> *next & 1 != 0;
            *next += 1;
            b
        }
    }
}

/// A name term's predicate: `^`/`$` anchors to the segment's start / end.
pub fn name_pat(t: &str) -> Pat {
    let l = lower_of(t);
    let (s, a) = match l.strip_prefix('^') {
        Some(r) => (r, true),
        None => (l.as_str(), false),
    };
    let (s, z) = match s.strip_suffix('$') {
        Some(r) => (r, true),
        None => (s, false),
    };
    let s = s.to_string();
    match (a, z) {
        (true, true) => Pat::Equals(s),
        (true, false) => Pat::Starts(s),
        (false, true) => Pat::Ends(s),
        (false, false) => Pat::Contains(s),
    }
}

pub fn compile(idx: &Index, q: &Query, parts: usize) -> Result<Compiled> {
    let scan = |id: &str| idx.scan(id).ok_or_else(|| format!("no scan {id}"));
    let (sa, sb) = match (&q.scan, &q.from, &q.to) {
        (Some(s), None, None) => (scan(s)?, None),
        (None, Some(a), Some(b)) => (scan(a)?, Some(scan(b)?)),
        _ => return err(format!("{}: give `scan`, or `from` and `to`", q.id)),
    };
    if q.depth == 0 {
        return err("depth must be ≥ 1");
    }
    let (dp, lo, hi) = if q.path.is_empty() {
        (0u8, 0usize, idx.n())
    } else {
        let g = idx.find(&q.path).ok_or_else(|| format!("{}: no path {}", q.id, q.path))?;
        (idx.depth[g], idx.group_end(g), idx.end[g] as usize + 1)
    };
    let search = match &q.q {
        None => None,
        Some(e) => {
            let mut ls = Vec::new();
            leaves(e, &mut ls);
            if ls.len() > 16 {
                return err("more than 16 search atoms");
            }
            let mut name_atoms = Vec::new();
            let mut path_atoms = Vec::new();
            let mut dict_bytes = 0u64;
            for (bit, leaf) in ls.iter().enumerate() {
                let bit = bit as u32;
                match leaf {
                    Expr::T(t) => {
                        name_atoms.push((bit, idx.dict.bitset(&name_pat(t), parts)));
                        dict_bytes += idx.dict.lower.len() as u64;
                    }
                    Expr::Re(r) => {
                        let re = regex::bytes::Regex::new(r)?;
                        name_atoms.push((bit, idx.dict.regex_bitset(&re, parts)));
                        dict_bytes += idx.dict.lower.len() as u64;
                    }
                    Expr::P(p) => {
                        let l = lower_of(p);
                        let (s, a) = match l.strip_prefix('^') {
                            Some(r) => (r, true),
                            None => (l.as_str(), false),
                        };
                        let (s, z) = match s.strip_suffix('$') {
                            Some(r) => (r, true),
                            None => (s, false),
                        };
                        let segs: Vec<&str> = s.split('/').collect();
                        if segs.len() < 2 {
                            // No `/`: a name term.
                            name_atoms.push((bit, idx.dict.bitset(&name_pat(p), parts)));
                            dict_bytes += idx.dict.lower.len() as u64;
                            continue;
                        }
                        let k = segs.len();
                        let pats: Vec<Pat> = segs
                            .iter()
                            .enumerate()
                            .map(|(j, s)| {
                                let s = s.to_string();
                                if j == 0 && j == k - 1 {
                                    unreachable!()
                                } else if j == 0 {
                                    if a { Pat::Equals(s) } else { Pat::Ends(s) }
                                } else if j == k - 1 {
                                    if z { Pat::Equals(s) } else { Pat::Starts(s) }
                                } else {
                                    Pat::Equals(s)
                                }
                            })
                            .collect();
                        let parts_bs = pats.iter().map(|p| idx.dict.bitset(p, parts)).collect();
                        dict_bytes += idx.dict.lower.len() as u64 * k as u64;
                        path_atoms.push(PathAtom { bit, parts: parts_bs });
                    }
                    _ => unreachable!(),
                }
            }
            if path_atoms.iter().any(|a| a.parts.len() > 32) {
                return err("a path term of more than 32 segments");
            }
            let lut = (0..1u32 << ls.len()).map(|m| eval(e, m, &mut 0)).collect();
            Some(Search { name_atoms, path_atoms, lut, dict_bytes })
        }
    };
    let owners = q.owners.as_ref().map(|sel| {
        let (names, keep) = match sel {
            OwnerSel::In(v) => (v, true),
            OwnerSel::NotIn(v) => (v, false),
        };
        idx.owners.iter().map(|o| names.contains(o) == keep).collect()
    });
    let classes = match &q.classes {
        None => None,
        Some(v) => {
            let mut m = 0u8;
            for &k in v {
                if !(1..=4).contains(&k) {
                    return err(format!("class {k}: classes are 1..4"));
                }
                m |= 1 << (k - 1);
            }
            Some(m)
        }
    };
    let staged = q.staged.as_ref().map(|ps| {
        let mut iv: Vec<(u32, u32)> = ps.iter().filter_map(|p| idx.find(p)).map(|g| (g as u32, idx.end[g])).collect();
        iv.sort_unstable();
        let mut out: Vec<(u32, u32)> = Vec::new();
        for (a, b) in iv {
            match out.last_mut() {
                Some(l) if a <= l.1 + 1 => l.1 = l.1.max(b),
                _ => out.push((a, b)),
            }
        }
        out
    });
    Ok(Compiled {
        sa,
        sb,
        dp,
        lo,
        hi,
        path: q.path.clone(),
        maxd: dp.saturating_add(q.depth),
        min_ppm: q.min_ppm,
        search,
        owners,
        classes,
        staged,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expr_json() {
        let e: Expr = serde_json::from_str(r#"{"and": [{"t": "ckpt"}, {"not": {"p": "a/b"}}, {"re": "^x"}]}"#).unwrap();
        assert_eq!(e, Expr::And(vec![Expr::T("ckpt".into()), Expr::Not(Box::new(Expr::P("a/b".into()))), Expr::Re("^x".into())]));
        let o: OwnerSel = serde_json::from_str(r#"{"not_in": ["a", ""]}"#).unwrap();
        assert_eq!(o, OwnerSel::NotIn(vec!["a".into(), "".into()]));
    }

    #[test]
    fn lut() {
        let e = Expr::And(vec![Expr::T("a".into()), Expr::Not(Box::new(Expr::Or(vec![Expr::T("b".into()), Expr::T("c".into())])))]);
        let got: Vec<bool> = (0..8).map(|m| eval(&e, m, &mut 0)).collect();
        assert_eq!(got, vec![false, true, false, false, false, false, false, false]);
    }

    #[test]
    fn anchors() {
        assert_eq!(
            ["^Ab", "ab$", "^ab$", "aB"].iter().map(|t| name_pat(t)).collect::<Vec<_>>(),
            vec![Pat::Starts("ab".into()), Pat::Ends("ab".into()), Pat::Equals("ab".into()), Pat::Contains("ab".into())]
        );
    }
}
