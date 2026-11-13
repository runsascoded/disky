//! One bounded pass over the slim index (`slim.rs`): a walk of the folder's directories in the view's depth window,
//! with every run of object versions below it summed flat.
//!
//! A chunk of object rows `[lo, hi)` walks the directories whose first object falls in it, merged with the object rows:
//! the objects between two directory starts are the deepest open directory's direct children. Where a directory is at
//! the window's deepest level (and no search needs its descendants' names), its whole subtree is one flat sum over
//! `vf`/`vt`/`size`, never visiting the directories under it. A search walks every directory below (their names count),
//! except under a directory whose matched atoms already decide the expression for any descendant: then its subtree is
//! summed flat (decided true) or skipped (decided false).
//!
//! Same semantics, threshold and answers as the full index's pass (`scan.rs`), which the bench checks.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use rayon::prelude::*;
use serde::Serialize;

use crate::index::{CLS_SHIFT, COMPLEX};
use crate::query::{compile_classes, compile_owners, compile_search, Query, Search};
use crate::scan::{Answer, Timing};
use crate::slim::SlimIndex;
use crate::util::{err, Result};

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
struct Acc {
    bytes: u64,
    files: u64,
}

impl Acc {
    #[inline(always)]
    fn add(&mut self, o: Acc) {
        self.bytes += o.bytes;
        self.files += o.files;
    }
    fn nonzero(&self) -> bool {
        self.bytes != 0 || self.files != 0
    }
}

/// Decided: the expression's value is fixed for every superset of the matched atoms.
const UNDECIDED: u8 = 0;
const YES: u8 = 1;
const NO: u8 = 2;

/// An object path's node key (its first row); a directory's is its id.
const OBJ: u64 = 1 << 63;

pub struct SCompiled {
    pub sa: u16,
    pub sb: Option<u16>,
    pub dp: u8,
    pub folder: Option<usize>,
    pub lo: usize,
    pub hi: usize,
    pub path: String,
    pub maxd: u8,
    pub min_ppm: u64,
    pub search: Option<Search>,
    pub dec: Vec<u8>,
    pub owners: Option<Vec<bool>>,
    /// `owners` as 0 / 1 bytes, for the branchless filtered sum.
    pub own_u8: Option<Vec<u8>>,
    pub classes: Option<u8>,
    /// Merged `[a, b)` object-row spans of the staged prefixes.
    pub staged: Option<Vec<(u32, u32)>>,
}

impl SCompiled {
    fn filtered(&self) -> bool {
        self.owners.is_some() || self.classes.is_some() || self.staged.is_some()
    }
}

/// Per mask, whether the expression is decided for every superset (`YES` / `NO`) or not.
fn decided(lut: &[bool], k: usize) -> Vec<u8> {
    let n = lut.len();
    let (mut all_t, mut all_f) = (lut.to_vec(), lut.iter().map(|&x| !x).collect::<Vec<_>>());
    for m in (0..n).rev() {
        for b in 0..k {
            if m >> b & 1 == 0 {
                let s = m | 1 << b;
                all_t[m] &= all_t[s];
                all_f[m] &= all_f[s];
            }
        }
    }
    (0..n).map(|m| if all_t[m] { YES } else if all_f[m] { NO } else { UNDECIDED }).collect()
}

pub fn compile(ix: &SlimIndex, q: &Query, parts: usize) -> Result<SCompiled> {
    if q.dirs {
        return err(format!("{}: the slim index has no directory totals", q.id));
    }
    let scan = |id: &str| ix.scan(id).ok_or_else(|| format!("no scan {id}"));
    let (sa, sb) = match (&q.scan, &q.from, &q.to) {
        (Some(s), None, None) => (scan(s)?, None),
        (None, Some(a), Some(b)) => (scan(a)?, Some(scan(b)?)),
        _ => return err(format!("{}: give `scan`, or `from` and `to`", q.id)),
    };
    if q.depth == 0 {
        return err("depth must be ≥ 1");
    }
    // A path that isn't a directory holding object versions (never one, or an object path) has no rows: an empty
    // view, as on the full index. The slim index can't tell it from a path that doesn't exist.
    let (folder, dp, lo, hi) = match ix.find_dir(&q.path) {
        Ok(None) => (None, 0u8, 0, ix.n_o()),
        // The folder's own versions (a path also an object) aren't in its view.
        Ok(Some(j)) => (Some(j), ix.ddepth[j], ix.own_end(j), ix.dhi[j] as usize),
        Err(()) => (None, q.path.split('/').count() as u8, 0, 0),
    };
    let search = q.q.as_ref().map(|e| compile_search(&ix.dict, e, parts)).transpose()?;
    let dec = search.as_ref().map_or(Vec::new(), |s| decided(&s.lut, s.lut.len().trailing_zeros() as usize));
    let staged = q.staged.as_ref().map(|ps| {
        let mut iv: Vec<(u32, u32)> = ps.iter().filter_map(|p| ix.rows_of(p)).map(|(a, b)| (a as u32, b as u32)).collect();
        iv.sort_unstable();
        let mut out: Vec<(u32, u32)> = Vec::new();
        for (a, b) in iv {
            match out.last_mut() {
                Some(l) if a <= l.1 => l.1 = l.1.max(b),
                _ => out.push((a, b)),
            }
        }
        out
    });
    Ok(SCompiled {
        sa,
        sb,
        dp,
        folder,
        lo,
        hi,
        path: q.path.clone(),
        maxd: dp.saturating_add(q.depth),
        min_ppm: q.min_ppm,
        search,
        dec,
        owners: q.owners.as_ref().map(|sel| compile_owners(&ix.owners, sel)),
        own_u8: q.owners.as_ref().map(|sel| compile_owners(&ix.owners, sel).iter().map(|&b| b as u8).collect()),
        classes: q.classes.as_ref().map(|v| compile_classes(v)).transpose()?,
        staged,
    })
}

#[derive(Clone, Copy)]
struct Frame {
    node: u64,
    depth: u8,
    dhi: u32,
    dend: u32,
    dec: u8,
    partial: bool,
    a: Acc,
    b: Acc,
}

#[derive(Clone, Copy, Debug)]
struct NodeOut {
    key: u64,
    depth: u8,
    a: Acc,
    b: Acc,
}

/// What a pass read.
#[derive(Clone, Copy, Default, Debug, Serialize)]
pub struct Touched {
    /// Object rows whose `vf`, `vt`, `size` were read (under a search, only rows of matching paths).
    pub rows: u64,
    /// Rows whose `name` a search read (every row it walked).
    pub named: u64,
    /// Rows that also read the filter columns (`owner` + `cls` for an owner scope, `cls` for a class scope).
    pub filtered: u64,
    /// Rows of object paths that are nodes (also read `cont`).
    pub cont: u64,
    /// Directories visited (`ddepth`, `dend`, `dlo`, `dhi`; `dname` under a search).
    pub dirs: u64,
    /// Rows under a decided-false directory, never read.
    pub skipped: u64,
}

impl Touched {
    fn add(&mut self, o: &Touched) {
        self.rows += o.rows;
        self.named += o.named;
        self.filtered += o.filtered;
        self.cont += o.cont;
        self.dirs += o.dirs;
        self.skipped += o.skipped;
    }

    /// Bytes read: 12 per row, + 4 for `name`, + 3 / 1 for the owner / class columns, + ⅛ for `cont`; 13 per
    /// directory (+ 4 for its name under a search); + the dictionary bytes the search scanned.
    pub fn bytes(&self, c: &SCompiled) -> u64 {
        let filt = if c.owners.is_some() { 3.0 } else if c.classes.is_some() { 1.0 } else { 0.0 };
        let dn = if c.search.is_some() { 4.0 } else { 0.0 };
        let b = 12.0 * self.rows as f64 + 4.0 * self.named as f64 + filt * self.filtered as f64 + self.cont as f64 / 8.0 + (13.0 + dn) * self.dirs as f64;
        b as u64 + c.search.as_ref().map_or(0, |s| s.dict_bytes)
    }
}

#[derive(Default)]
struct ChunkOut {
    done: Vec<NodeOut>,
    partial: Vec<NodeOut>,
    a: Acc,
    b: Acc,
    errors: u64,
    t: Touched,
}

struct Ctx<'a> {
    ix: &'a SlimIndex,
    c: &'a SCompiled,
    ga: AtomicU64,
    gb: AtomicU64,
}

/// Object row `r`'s `cont` bit.
#[inline(always)]
fn cont_bit(cont: &[u64], r: usize) -> bool {
    unsafe { *cont.get_unchecked(r >> 6) >> (r & 63) & 1 != 0 }
}

/// The columns a chunk reads, as plain slices (no per-access `Col` dispatch in the loops).
#[derive(Clone, Copy)]
struct Cols<'a> {
    vf: &'a [u16],
    vt: &'a [u16],
    size: &'a [u64],
    name: &'a [u32],
    owner: &'a [u16],
    cls: &'a [u8],
    cont: &'a [u64],
    ddepth: &'a [u8],
    dname: &'a [u32],
    dlo: &'a [u32],
    dhi: &'a [u32],
    dend: &'a [u32],
}

impl<'a> Cols<'a> {
    fn of(ix: &'a SlimIndex) -> Cols<'a> {
        Cols {
            vf: &ix.vf,
            vt: &ix.vt,
            size: &ix.size,
            name: &ix.name,
            owner: &ix.owner,
            cls: &ix.cls,
            cont: &ix.cont,
            ddepth: &ix.ddepth,
            dname: &ix.dname,
            dlo: &ix.dlo,
            dhi: &ix.dhi,
            dend: &ix.dend,
        }
    }
}

struct Chunk<'a, const SEARCH: bool, const DIFF: bool, const FILT: bool> {
    ctx: &'a Ctx<'a>,
    k: Cols<'a>,
    lo: usize,
    hi: usize,
    st: Vec<Frame>,
    mask: [u32; 257],
    pstate: Vec<[u32; 257]>,
    out: ChunkOut,
    sp: usize,
    run_a: u64,
    run_b: u64,
    pub_a: u64,
    pub_b: u64,
    since: u64,
    lb: u128,
}

#[inline(never)]
fn sum_live(vf: &[u16], vt: &[u16], size: &[u64], s: u16) -> Acc {
    let (mut b, mut n) = (0u64, 0u64);
    for ((&f, &t), &z) in vf.iter().zip(vt).zip(size) {
        let l = ((f <= s) & (s < t)) as u64;
        b += z & l.wrapping_neg();
        n += l;
    }
    Acc { bytes: b, files: n }
}

/// `sum_live`(`2`) under an owner and / or class scope, for rows whose slices are whole (no complex row in the range):
/// a row counts when live and its owner's byte in `own` (when given) and its class's bit in `cm` (when given) are set.
#[inline(never)]
fn sum_filt<const DIFF: bool, const OWN: bool, const CLS: bool>(k: &Cols, a: usize, b: usize, own: &[u8], cm: u8, s: u16, s2: u16) -> (Acc, Acc) {
    let (mut x, mut y) = (Acc::default(), Acc::default());
    for r in a..b {
        let (f, t, z) = (k.vf[r], k.vt[r], k.size[r]);
        let mut ok = 1u64;
        if OWN {
            ok &= own[k.owner[r] as usize] as u64;
        }
        if CLS {
            ok &= ((cm >> ((k.cls[r] >> CLS_SHIFT) & 3)) & 1) as u64;
        }
        let l = ((f <= s) & (s < t)) as u64 & ok;
        x.bytes += z & l.wrapping_neg();
        x.files += l;
        if DIFF {
            let l2 = ((f <= s2) & (s2 < t)) as u64 & ok;
            y.bytes += z & l2.wrapping_neg();
            y.files += l2;
        }
    }
    (x, y)
}

#[inline(never)]
fn sum_live2(vf: &[u16], vt: &[u16], size: &[u64], s: u16, s2: u16) -> (Acc, Acc) {
    let (mut b, mut n, mut b2, mut n2) = (0u64, 0u64, 0u64, 0u64);
    for ((&f, &t), &z) in vf.iter().zip(vt).zip(size) {
        let l = ((f <= s) & (s < t)) as u64;
        let l2 = ((f <= s2) & (s2 < t)) as u64;
        b += z & l.wrapping_neg();
        n += l;
        b2 += z & l2.wrapping_neg();
        n2 += l2;
    }
    (Acc { bytes: b, files: n }, Acc { bytes: b2, files: n2 })
}

impl<'a, const SEARCH: bool, const DIFF: bool, const FILT: bool> Chunk<'a, SEARCH, DIFF, FILT> {
    #[inline(always)]
    fn atom_bits(&mut self, id: u32, d: usize) {
        let s: &Search = self.ctx.c.search.as_ref().unwrap();
        let mut m = self.mask[d - 1];
        for (bit, bs) in &s.name_atoms {
            if bs.get(id) {
                m |= 1 << bit;
            }
        }
        for (j, pa) in s.path_atoms.iter().enumerate() {
            let k = pa.parts.len();
            let mut sat = 0u32;
            for (i, bs) in pa.parts.iter().enumerate() {
                if bs.get(id) {
                    sat |= 1 << i;
                }
            }
            let st = (sat & 1) | ((self.pstate[j][d - 1] << 1) & sat);
            self.pstate[j][d] = st;
            if st >> (k - 1) & 1 != 0 {
                m |= 1 << pa.bit;
            }
        }
        self.mask[d] = m;
    }

    /// A passing row's `(bytes, count)`, `(0, 0)` when it fails a filter (the full pass's `filtered`).
    #[inline]
    fn filtered(&mut self, r: usize) -> (u64, u64) {
        let c = self.ctx.c;
        let ix = self.ctx.ix;
        if let Some(st) = &c.staged {
            while self.sp < st.len() && (st[self.sp].1 as usize) <= r {
                self.sp += 1;
            }
            if !(self.sp < st.len() && st[self.sp].0 as usize <= r) {
                return (0, 0);
            }
        }
        let k = self.k;
        let f = k.cls[r];
        if f & COMPLEX == 0 {
            let own = c.owners.as_ref().map_or(true, |o| o[k.owner[r] as usize]);
            let cls = c.classes.map_or(true, |m| m >> ((f >> CLS_SHIFT) & 3) & 1 != 0);
            return if own && cls { (k.size[r], 1) } else { (0, 0) };
        }
        let cx = ix.complex_of(r);
        let (own_sel, own_pass) = match &c.owners {
            None => (ix.size[r], true),
            Some(o) => {
                let sel = cx.owners.iter().filter(|s| o[s.0 as usize]);
                (sel.clone().map(|s| s.1).sum(), sel.count() > 0)
            }
        };
        let (cls_sel, cls_pass) = match c.classes {
            None => (ix.size[r], true),
            Some(m) => ((0..4).filter(|k| m >> k & 1 != 0 && cx.cls_mask >> k & 1 != 0).map(|k| cx.cls[k]).sum(), cx.cls_mask & m != 0),
        };
        let bytes = match (c.owners.is_some(), c.classes.is_some()) {
            (false, false) => ix.size[r],
            (true, false) => own_sel,
            (false, true) => cls_sel,
            (true, true) => {
                if cx.cls_mask.count_ones() == 1 {
                    if cls_pass { own_sel } else { 0 }
                } else if cx.owners.len() == 1 {
                    if own_pass { cls_sel } else { 0 }
                } else {
                    self.out.errors += 1;
                    0
                }
            }
        };
        if own_pass && cls_pass { (bytes, 1) } else { (0, 0) }
    }

    #[inline(always)]
    fn target(&self, ti: usize) -> usize {
        ti.min((self.ctx.c.maxd - self.ctx.c.dp) as usize)
    }

    fn publish(&mut self, rows: u64) {
        self.since += rows;
        if self.since < 1 << 16 || self.ctx.c.min_ppm == 0 {
            return;
        }
        self.since = 0;
        let ta = self.ctx.ga.fetch_add(self.run_a - self.pub_a, Ordering::Relaxed) + self.run_a - self.pub_a;
        let tb = if DIFF { self.ctx.gb.fetch_add(self.run_b - self.pub_b, Ordering::Relaxed) + self.run_b - self.pub_b } else { 0 };
        self.pub_a = self.run_a;
        self.pub_b = self.run_b;
        self.lb = self.ctx.c.min_ppm as u128 * ta.max(tb) as u128;
    }

    /// Rows `[a, b)` summed into frame `t`; `row_search`: each row's own name decides (the rows are direct children of
    /// the top frame, at depth `d`).
    fn flat(&mut self, t: usize, a: usize, b: usize, row_search: bool, d: usize) {
        let k = self.k;
        let c = self.ctx.c;
        let (sa, sb) = (c.sa, c.sb.unwrap_or(0));
        let n = (b - a) as u64;
        if !row_search {
            self.out.t.rows += n;
        }
        if !FILT && !row_search {
            let (x, y) = if DIFF {
                sum_live2(&k.vf[a..b], &k.vt[a..b], &k.size[a..b], sa, sb)
            } else {
                (sum_live(&k.vf[a..b], &k.vt[a..b], &k.size[a..b], sa), Acc::default())
            };
            self.st[t].a.add(x);
            self.run_a += x.bytes;
            if DIFF {
                self.st[t].b.add(y);
                self.run_b += y.bytes;
            }
            self.publish((b - a) as u64);
            return;
        }
        if FILT && !row_search {
            self.out.t.filtered += n;
        }
        let ix = self.ctx.ix;
        let cx = ix.complex_rows.partition_point(|&r| (r as usize) < a);
        if FILT && !row_search && !(cx < ix.complex_rows.len() && (ix.complex_rows[cx] as usize) < b) {
            // Every row a whole slice: branchless, a staged scope as the row spans it keeps.
            let own: &[u8] = c.own_u8.as_deref().unwrap_or(&[]);
            let cm = c.classes.unwrap_or(0);
            let sum = |lo: usize, hi: usize| -> (Acc, Acc) {
                match (c.owners.is_some(), c.classes.is_some()) {
                    (true, true) => sum_filt::<DIFF, true, true>(&k, lo, hi, own, cm, sa, sb),
                    (true, false) => sum_filt::<DIFF, true, false>(&k, lo, hi, own, cm, sa, sb),
                    (false, true) => sum_filt::<DIFF, false, true>(&k, lo, hi, own, cm, sa, sb),
                    (false, false) => sum_filt::<DIFF, false, false>(&k, lo, hi, own, cm, sa, sb),
                }
            };
            let (mut x, mut y) = (Acc::default(), Acc::default());
            match &c.staged {
                None => {
                    let (p, q) = sum(a, b);
                    x.add(p);
                    y.add(q);
                }
                Some(st) => {
                    // Only the spans' rows are read.
                    self.out.t.rows -= n;
                    self.out.t.filtered -= n;
                    let mut i = st.partition_point(|s| (s.1 as usize) <= a);
                    while i < st.len() && (st[i].0 as usize) < b {
                        let (lo, hi) = ((st[i].0 as usize).max(a), (st[i].1 as usize).min(b));
                        self.out.t.rows += (hi - lo) as u64;
                        self.out.t.filtered += (hi - lo) as u64;
                        let (p, q) = sum(lo, hi);
                        x.add(p);
                        y.add(q);
                        i += 1;
                    }
                }
            }
            self.st[t].a.add(x);
            self.run_a += x.bytes;
            if DIFF {
                self.st[t].b.add(y);
                self.run_b += y.bytes;
            }
            self.publish(n);
            return;
        }
        if row_search {
            self.out.t.named += n;
        }
        let lut = c.search.as_ref().map(|s| &s.lut[..]).unwrap_or(&[]);
        let mut pass = true;
        let (mut x, mut y) = (Acc::default(), Acc::default());
        for r in a..b {
            if row_search && (r == a || !cont_bit(k.cont, r)) {
                self.atom_bits(k.name[r], d);
                pass = lut[self.mask[d] as usize];
            }
            if row_search && !pass {
                continue;
            }
            if row_search {
                self.out.t.rows += 1;
                if FILT {
                    self.out.t.filtered += 1;
                }
            }
            let (f0, t0) = (k.vf[r], k.vt[r]);
            let la = f0 <= sa && sa < t0;
            let lbv = DIFF && f0 <= sb && sb < t0;
            if !(la || lbv) {
                continue;
            }
            let (bytes, cnt) = if FILT {
                let v = self.filtered(r);
                if v.1 == 0 {
                    continue;
                }
                v
            } else {
                (k.size[r], 1)
            };
            if la {
                x.bytes += bytes;
                x.files += cnt;
            }
            if lbv {
                y.bytes += bytes;
                y.files += cnt;
            }
        }
        self.st[t].a.add(x);
        self.run_a += x.bytes;
        if DIFF {
            self.st[t].b.add(y);
            self.run_b += y.bytes;
        }
        self.publish((b - a) as u64);
    }

    /// A search below the window: frame `ti` (at the deepest level or below, undecided) from object row `o` to `e`, the
    /// directories from `j` on. One loop over objects and directories with per-depth bounds instead of frames, every
    /// passing row summed into the window node; a directory whose atoms decide the expression is summed flat or skipped
    /// whole. Returns where it stopped: `(o, j)`.
    fn deep(&mut self, ti: usize, mut o: usize, e: usize, mut j: usize) -> (usize, usize) {
        let k = self.k;
        let ix = self.ctx.ix;
        let c = self.ctx.c;
        let (sa, sb) = (c.sa, c.sb.unwrap_or(0));
        let nd = k.ddepth.len();
        let t = self.target(ti);
        let d0 = self.st[ti].depth as usize;
        let lut: &[bool] = &c.search.as_ref().unwrap().lut;
        let mut hi_at = [0usize; 257];
        let mut cur = d0;
        let (mut x, mut y) = (Acc::default(), Acc::default());
        let (mut rows, mut dirs, mut passed) = (0u64, 0u64, 0u64);
        loop {
            while cur > d0 && hi_at[cur] <= o {
                cur -= 1;
            }
            let bound = if cur > d0 { hi_at[cur].min(e) } else { e };
            let next = if j < nd && (k.dlo[j] as usize) < bound { k.dlo[j] as usize } else { bound };
            // `cur`'s direct objects.
            let dd = cur + 1;
            let mut pass = false;
            for r in o..next {
                if r == o || !cont_bit(k.cont, r) {
                    self.atom_bits(k.name[r], dd);
                    pass = lut[self.mask[dd] as usize];
                }
                if !pass {
                    continue;
                }
                passed += 1;
                let (f0, t0) = (k.vf[r], k.vt[r]);
                let la = f0 <= sa && sa < t0;
                let lbv = DIFF && f0 <= sb && sb < t0;
                if !(la || lbv) {
                    continue;
                }
                let (bytes, cnt) = if FILT {
                    let v = self.filtered(r);
                    if v.1 == 0 {
                        continue;
                    }
                    v
                } else {
                    (k.size[r], 1)
                };
                if la {
                    x.bytes += bytes;
                    x.files += cnt;
                }
                if lbv {
                    y.bytes += bytes;
                    y.files += cnt;
                }
            }
            rows += (next - o) as u64;
            o = next;
            if o >= e {
                break;
            }
            if o == bound {
                continue;
            }
            // Enter directory `j`, a child of `cur`.
            let dj = cur + 1;
            self.atom_bits(k.dname[j], dj);
            dirs += 1;
            let jhi = k.dhi[j] as usize;
            match c.dec[self.mask[dj] as usize] {
                NO => {
                    let z = jhi.min(e);
                    self.out.t.skipped += (z - o) as u64;
                    o = z;
                    j = k.dend[j] as usize + 1;
                }
                YES => {
                    let z = jhi.min(e);
                    self.flat(t, o, z, false, 0);
                    o = z;
                    j = k.dend[j] as usize + 1;
                }
                _ => {
                    if !ix.dself.is_empty() {
                        // Its own versions: the directory's path's atoms decide them.
                        let z = ix.own_end(j).min(e);
                        if z > o {
                            if lut[self.mask[dj] as usize] {
                                self.flat(t, o, z, false, 0);
                            }
                            o = z;
                        }
                    }
                    cur = dj;
                    hi_at[dj] = jhi;
                    j += 1;
                }
            }
        }
        self.out.t.rows += passed;
        self.out.t.named += rows;
        if FILT {
            self.out.t.filtered += passed;
        }
        self.out.t.dirs += dirs;
        self.st[t].a.add(x);
        self.run_a += x.bytes;
        if DIFF {
            self.st[t].b.add(y);
            self.run_b += y.bytes;
        }
        self.publish(rows);
        (o, j)
    }

    /// Rows `[a, b)`: direct children of frame `ti` at a depth in the window, each object path a node.
    fn obj_nodes(&mut self, ti: usize, a: usize, b: usize) {
        let ix = self.ctx.ix;
        let k = self.k;
        let c = self.ctx.c;
        let (sa, sb) = (c.sa, c.sb.unwrap_or(0));
        let d = self.st[ti].depth as usize + 1;
        let need_search = SEARCH && self.st[ti].dec == UNDECIDED;
        let n = (b - a) as u64;
        self.out.t.rows += n;
        self.out.t.cont += n;
        if need_search {
            self.out.t.named += n;
        }
        if FILT {
            self.out.t.filtered += n;
        }
        let mut r = a;
        while r < b {
            let start = if r == self.lo && cont_bit(k.cont, r) { ix.obj_rows(r).0 } else { r };
            let mut e = r + 1;
            while e < b && cont_bit(k.cont, e) {
                e += 1;
            }
            let continues = e == self.hi && e < k.vf.len() && cont_bit(k.cont, e);
            let pass = if need_search {
                self.atom_bits(k.name[r], d);
                c.search.as_ref().unwrap().lut[self.mask[d] as usize]
            } else {
                true
            };
            let (mut x, mut y) = (Acc::default(), Acc::default());
            if pass {
                for v in r..e {
                    let (f0, t0) = (k.vf[v], k.vt[v]);
                    let la = f0 <= sa && sa < t0;
                    let lbv = DIFF && f0 <= sb && sb < t0;
                    if !(la || lbv) {
                        continue;
                    }
                    let (bytes, cnt) = if FILT {
                        let q = self.filtered(v);
                        if q.1 == 0 {
                            continue;
                        }
                        q
                    } else {
                        (k.size[v], 1)
                    };
                    if la {
                        x.bytes += bytes;
                        x.files += cnt;
                    }
                    if lbv {
                        y.bytes += bytes;
                        y.files += cnt;
                    }
                }
            }
            if x.nonzero() || y.nonzero() {
                let n = NodeOut { key: OBJ | start as u64, depth: d as u8, a: x, b: y };
                if start < self.lo || continues {
                    self.out.partial.push(n);
                } else if (x.bytes.max(y.bytes) as u128) * 1_000_000 >= self.lb {
                    self.out.done.push(n);
                }
                let f = &mut self.st[ti];
                f.a.add(x);
                self.run_a += x.bytes;
                if DIFF {
                    f.b.add(y);
                    self.run_b += y.bytes;
                }
            }
            // The lower bound moves within a run too: a flat directory's millions of small objects are dropped
            // here, not handed to the assembly.
            self.publish((e - r) as u64);
            r = e;
        }
    }

    fn pop(&mut self) {
        let f = self.st.pop().unwrap();
        if f.depth > self.ctx.c.maxd {
            return;
        }
        let p = self.st.last_mut().unwrap();
        p.a.add(f.a);
        if DIFF {
            p.b.add(f.b);
        }
        if f.a.nonzero() || f.b.nonzero() {
            let n = NodeOut { key: f.node, depth: f.depth, a: f.a, b: f.b };
            if f.partial || f.dhi as usize > self.hi {
                self.out.partial.push(n);
            } else if (f.a.bytes.max(f.b.bytes) as u128) * 1_000_000 >= self.lb {
                self.out.done.push(n);
            }
        }
    }

    /// A walked frame's own object versions `[a, b)` (a path that is also a directory): the node's own bytes, matched
    /// by its own path's atoms.
    fn own(&mut self, ti: usize, a: usize, b: usize) {
        if SEARCH && self.st[ti].dec == UNDECIDED && !self.ctx.c.search.as_ref().unwrap().lut[self.mask[self.st[ti].depth as usize] as usize] {
            return;
        }
        let t = self.target(ti);
        self.flat(t, a, b, false, 0);
    }

    /// Push directory `j` (a child of the top frame).
    fn push(&mut self, j: usize, partial: bool) {
        let k = self.k;
        let d = k.ddepth[j];
        let parent_dec = self.st.last().unwrap().dec;
        let mut dec = parent_dec;
        if SEARCH && parent_dec == UNDECIDED {
            self.atom_bits(k.dname[j], d as usize);
            dec = self.ctx.c.dec[self.mask[d as usize] as usize];
        }
        self.out.t.dirs += 1;
        self.st.push(Frame { node: j as u64, depth: d, dhi: k.dhi[j], dend: k.dend[j], dec, partial, a: Acc::default(), b: Acc::default() });
    }

    /// Whether frame `ti` sums its rows flat (`Some(true)`), skips them (`Some(false)`), or is walked (`None`).
    #[inline]
    fn mode(&self, ti: usize) -> Option<bool> {
        let f = &self.st[ti];
        if SEARCH && f.dec == NO {
            return Some(false);
        }
        if ti > 0 && f.depth >= self.ctx.c.maxd && (!SEARCH || f.dec == YES) {
            return Some(true);
        }
        None
    }

    fn run(mut self) -> ChunkOut {
        let ix = self.ctx.ix;
        let c = self.ctx.c;
        let (lo, hi) = (self.lo, self.hi);
        let nd = ix.n_d();
        // The first directory starting in this chunk, never the folder or above.
        let c0 = ix.dlo.partition_point(|&x| (x as usize) < lo);
        let mut j = c.folder.map_or(c0, |f| c0.max(f + 1));
        let chain = ix.chain_before(j, lo);
        self.st.push(Frame { node: u64::MAX, depth: c.dp, dhi: u32::MAX, dend: u32::MAX, dec: UNDECIDED, partial: true, a: Acc::default(), b: Acc::default() });
        // The folder and its ancestors give the search its first matched atoms; the chain below it is pushed as
        // partial frames, down to the first frame that sums flat or skips.
        if SEARCH {
            for &g in chain.iter().filter(|&&g| ix.ddepth[g as usize] <= c.dp) {
                self.atom_bits(ix.dname[g as usize], ix.ddepth[g as usize] as usize);
            }
            self.st[0].dec = c.dec[self.mask[c.dp as usize] as usize];
        }
        for &g in chain.iter().filter(|&&g| ix.ddepth[g as usize] > c.dp) {
            if self.mode(self.st.len() - 1).is_some() {
                break;
            }
            self.push(g as usize, true);
        }
        // The threshold's lower bound as other chunks have already published it.
        if c.min_ppm > 0 {
            let g = self.ctx.ga.load(Ordering::Relaxed).max(self.ctx.gb.load(Ordering::Relaxed));
            self.lb = c.min_ppm as u128 * g as u128;
        }
        let mut o = lo;
        // The chunk may start inside the deepest open directory's own versions.
        let ti = self.st.len() - 1;
        if ti > 0 && self.mode(ti).is_none() {
            let e = ix.own_end(self.st[ti].node as usize).min(hi);
            if e > o {
                self.own(ti, o, e);
                o = e;
            }
        }
        loop {
            let ti = self.st.len() - 1;
            let (dhi, dend) = (self.st[ti].dhi as usize, self.st[ti].dend as usize);
            if let Some(sum) = self.mode(ti) {
                let e = dhi.min(hi);
                if e > o {
                    if sum {
                        let t = self.target(ti);
                        self.flat(t, o, e, false, 0);
                    } else {
                        self.out.t.skipped += (e - o) as u64;
                    }
                }
                o = e;
                if o == dhi && ti > 0 {
                    j = j.max(dend + 1);
                    self.pop();
                    continue;
                }
                break;
            }
            if SEARCH && ti > 0 && self.st[ti].depth >= c.maxd {
                (o, j) = self.deep(ti, o, dhi.min(hi), j);
                if o == dhi {
                    self.pop();
                    continue;
                }
                break;
            }
            let t = if j < nd && (self.k.dlo[j] as usize) < hi { self.k.dlo[j] as usize } else { hi };
            let e = t.min(dhi);
            if e > o {
                let d = self.st[ti].depth;
                if d < c.maxd {
                    self.obj_nodes(ti, o, e);
                } else {
                    let tg = self.target(ti);
                    self.flat(tg, o, e, SEARCH, d as usize + 1);
                }
                o = e;
            }
            if o == dhi && ti > 0 {
                self.pop();
                continue;
            }
            if o >= hi {
                break;
            }
            self.push(j, false);
            let ti = self.st.len() - 1;
            if !ix.dself.is_empty() && self.mode(ti).is_none() {
                let e = ix.own_end(j).min(hi);
                if e > o {
                    self.own(ti, o, e);
                    o = e;
                }
            }
            j += 1;
        }
        self.publish(1 << 16);
        while self.st.len() > 1 {
            let f = self.st.last_mut().unwrap();
            f.partial = true;
            self.pop();
        }
        self.out.a = self.st[0].a;
        self.out.b = self.st[0].b;
        self.out
    }
}

impl Ctx<'_> {
    fn chunk_any(&self, lo: usize, hi: usize) -> ChunkOut {
        let (s, d, f) = (self.c.search.is_some(), self.c.sb.is_some(), self.c.filtered());
        macro_rules! go {
            ($s:literal, $d:literal, $f:literal) => {{
                let n_path = self.c.search.as_ref().map_or(0, |s| s.path_atoms.len());
                Chunk::<$s, $d, $f> {
                    ctx: self,
                    k: Cols::of(self.ix),
                    lo,
                    hi,
                    st: Vec::with_capacity(64),
                    mask: [0; 257],
                    pstate: vec![[0; 257]; n_path],
                    out: ChunkOut::default(),
                    sp: 0,
                    run_a: 0,
                    run_b: 0,
                    pub_a: 0,
                    pub_b: 0,
                    since: 0,
                    lb: 0,
                }
                .run()
            }};
        }
        match (s, d, f) {
            (false, false, false) => go!(false, false, false),
            (false, false, true) => go!(false, false, true),
            (false, true, false) => go!(false, true, false),
            (false, true, true) => go!(false, true, true),
            (true, false, false) => go!(true, false, false),
            (true, false, true) => go!(true, false, true),
            (true, true, false) => go!(true, true, false),
            (true, true, true) => go!(true, true, true),
        }
    }
}

/// The smallest chunk, in object rows: 64K, or `$FSB_CHUNK_MIN` (tests set it tiny, to put chunk edges everywhere).
fn chunk_min() -> usize {
    std::env::var("FSB_CHUNK_MIN").ok().and_then(|v| v.parse().ok()).unwrap_or(1 << 16)
}

/// Answer `q` on `pool`'s threads; also what it read.
pub fn run(ix: &SlimIndex, q: &Query, pool: &rayon::ThreadPool) -> Result<(Answer, Timing, Touched)> {
    let t0 = Instant::now();
    let threads = pool.current_num_threads();
    let c = pool.install(|| compile(ix, q, threads))?;
    let compile_ms = t0.elapsed().as_secs_f64() * 1e3;
    let t1 = Instant::now();
    let len = c.hi.saturating_sub(c.lo);
    let step = (len.div_ceil(threads * 8)).max(chunk_min());
    let spans: Vec<(usize, usize)> = (c.lo..c.hi).step_by(step.max(1)).map(|a| (a, (a + step).min(c.hi))).collect();
    let ctx = Ctx { ix, c: &c, ga: AtomicU64::new(0), gb: AtomicU64::new(0) };
    let outs: Vec<ChunkOut> = pool.install(|| spans.par_iter().map(|&(a, b)| ctx.chunk_any(a, b)).collect());
    let pass_ms = t1.elapsed().as_secs_f64() * 1e3;
    let t2 = Instant::now();
    let errors: u64 = outs.iter().map(|o| o.errors).sum();
    if errors > 0 {
        return err(format!("{}: {errors} files hold several owner and several class slices", q.id));
    }
    let mut touched = Touched::default();
    outs.iter().for_each(|o| touched.add(&o.t));
    let answer = assemble(ix, &c, q, outs);
    let assemble_ms = t2.elapsed().as_secs_f64() * 1e3;
    let timing = Timing {
        compile_ms,
        pass_ms,
        assemble_ms,
        total_ms: t0.elapsed().as_secs_f64() * 1e3,
        rows: touched.rows,
        bytes: touched.bytes(&c),
        chunks: spans.len() as u64,
    };
    Ok((answer, timing, touched))
}

fn assemble(ix: &SlimIndex, c: &SCompiled, q: &Query, outs: Vec<ChunkOut>) -> Answer {
    let (mut ta, mut tb) = (Acc::default(), Acc::default());
    let mut nodes: Vec<NodeOut> = Vec::new();
    let mut partial: HashMap<u64, NodeOut> = HashMap::new();
    for o in outs {
        ta.add(o.a);
        tb.add(o.b);
        nodes.extend(o.done);
        for p in o.partial {
            partial
                .entry(p.key)
                .and_modify(|x| {
                    x.a.add(p.a);
                    x.b.add(p.b);
                })
                .or_insert(p);
        }
    }
    nodes.extend(partial.into_values());
    let thr = c.min_ppm as u128 * ta.bytes.max(tb.bytes) as u128;
    nodes.retain(|n| (n.a.nonzero() || n.b.nonzero()) && (n.a.bytes.max(n.b.bytes) as u128) * 1_000_000 >= thr);
    // Pre-order: a directory at its first object row, before that row; nested directories by id.
    let pos = |k: u64| -> (u64, u8, u64) {
        if k & OBJ != 0 {
            (k & !OBJ, 1, 0)
        } else {
            (ix.dlo[k as usize] as u64, 0, k)
        }
    };
    nodes.sort_unstable_by_key(|n| pos(n.key));

    struct Open {
        depth: u8,
        /// Object rows below it end before this row.
        hi: u64,
        path: String,
        a: Acc,
        b: Acc,
        kids_a: Acc,
        kids_b: Acc,
    }
    let mut out_nodes = Vec::with_capacity(nodes.len());
    let mut rest = Vec::new();
    let mut stack: Vec<Open> = vec![Open { depth: c.dp, hi: u64::MAX, path: c.path.clone(), a: ta, b: tb, kids_a: Acc::default(), kids_b: Acc::default() }];
    let close = |o: Open, rest: &mut Vec<(String, i64, i64, i64, i64)>| {
        if o.depth < c.maxd {
            let d = |x: u64, y: u64| x as i64 - y as i64;
            let (ra, rb) = (d(o.a.bytes, o.kids_a.bytes), d(o.b.bytes, o.kids_b.bytes));
            let (fa, fb) = (d(o.a.files, o.kids_a.files), d(o.b.files, o.kids_b.files));
            if ra != 0 || rb != 0 || fa != 0 || fb != 0 {
                rest.push((o.path, ra, fa, rb, fb));
            }
        }
    };
    for n in nodes {
        let (p, _, _) = pos(n.key);
        while stack.len() > 1 && (stack.last().unwrap().depth >= n.depth || stack.last().unwrap().hi <= p) {
            let o = stack.pop().unwrap();
            close(o, &mut rest);
        }
        let (name_id, hi) = if n.key & OBJ != 0 { (ix.name[p as usize], p + 1) } else { (ix.dname[n.key as usize], ix.dhi[n.key as usize] as u64) };
        let parent = stack.last_mut().unwrap();
        let path = if parent.depth + 1 == n.depth {
            parent.kids_a.add(n.a);
            parent.kids_b.add(n.b);
            let name = ix.dict.orig(name_id);
            if parent.path.is_empty() { name.to_string() } else { format!("{}/{}", parent.path, name) }
        } else if n.key & OBJ != 0 {
            ix.obj_path(p as usize)
        } else {
            ix.dir_path(n.key as usize)
        };
        out_nodes.push((path.clone(), n.a.bytes, n.a.files, n.b.bytes, n.b.files));
        stack.push(Open { depth: n.depth, hi, path, a: n.a, b: n.b, kids_a: Acc::default(), kids_b: Acc::default() });
    }
    while let Some(o) = stack.pop() {
        close(o, &mut rest);
    }
    out_nodes.sort();
    rest.sort();
    Answer { id: q.id.clone(), total: [ta.bytes, ta.files, tb.bytes, tb.files], nodes: out_nodes, rest }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decided_masks() {
        // a AND NOT b over bits (a = 1, b = 2): {} undecided, {a} undecided, {b} no, {a, b} no.
        let lut = vec![false, true, false, false];
        assert_eq!(decided(&lut, 2), vec![UNDECIDED, UNDECIDED, NO, NO]);
        // a OR b: {} undecided, every nonempty set yes.
        assert_eq!(decided(&[false, true, true, true], 2), vec![UNDECIDED, YES, YES, YES]);
    }
}
