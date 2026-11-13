//! One bounded pass over a folder's rows: depth-first, a stack of the open nodes within the view's depth window, each
//! live, passing file version added to the deepest open node, each node rolled into its parent when it closes.
//!
//! Threads split the folder's row range into chunks. A chunk starts from its first row's ancestor chain (a checkpoint
//! every `CK` rows, replayed to the chunk's start), so its search masks and open nodes are known without a scan from the
//! root. A node a chunk saw only part of is a partial, summed across chunks; a node wholly inside one chunk is emitted
//! there, and dropped when it is already under the threshold's lower bound (the bytes all chunks have summed so far).

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use rayon::prelude::*;
use serde::Serialize;

use crate::index::{Index, CLS_SHIFT, COMPLEX, CONT, FILE};
use crate::query::{compile, Compiled, Query};
use crate::util::{err, Result};

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub struct Acc {
    pub bytes: u64,
    pub files: u64,
}

impl Acc {
    fn add(&mut self, o: Acc) {
        self.bytes += o.bytes;
        self.files += o.files;
    }
    fn nonzero(&self) -> bool {
        self.bytes != 0 || self.files != 0
    }
}

#[derive(Clone, Copy)]
struct Frame {
    node: u32,
    depth: u8,
    partial: bool,
    a: Acc,
    b: Acc,
}

#[derive(Clone, Copy, Debug)]
struct NodeOut {
    row: u32,
    depth: u8,
    a: Acc,
    b: Acc,
}

#[derive(Default)]
struct ChunkOut {
    done: Vec<NodeOut>,
    partial: Vec<NodeOut>,
    a: Acc,
    b: Acc,
    errors: u64,
}

/// The answer: paths with `[bytes, files]` at `a` (the scan, or a diff's `from`) and `b` (a diff's `to`; 0 for a view).
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct Answer {
    pub id: String,
    pub total: [u64; 4],
    pub nodes: Vec<(String, u64, u64, u64, u64)>,
    /// Signed: from directory totals a store whose rows don't add up can leave a negative rest.
    pub rest: Vec<(String, i64, i64, i64, i64)>,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Timing {
    pub compile_ms: f64,
    pub pass_ms: f64,
    pub assemble_ms: f64,
    pub total_ms: f64,
    pub rows: u64,
    pub bytes: u64,
    pub chunks: u64,
}

struct Ctx<'a> {
    idx: &'a Index,
    c: &'a Compiled,
    ga: AtomicU64,
    gb: AtomicU64,
}

impl Ctx<'_> {
    #[inline]
    fn atom_bits(&self, r: usize, d: usize, mask: &mut [u32; 257], pstate: &mut [[u32; 257]]) {
        let s = self.c.search.as_ref().unwrap();
        let id = self.idx.name[r];
        let mut m = mask[d - 1];
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
            let st = (sat & 1) | ((pstate[j][d - 1] << 1) & sat);
            pstate[j][d] = st;
            if st >> (k - 1) & 1 != 0 {
                m |= 1 << pa.bit;
            }
        }
        mask[d] = m;
    }

    /// A passing file's `(bytes, count)`, `(0, 0)` when it fails a filter.
    #[inline]
    fn filtered(&self, r: usize, f: u8, staged_ok: bool, errors: &mut u64) -> (u64, u64) {
        if !staged_ok {
            return (0, 0);
        }
        let c = self.c;
        let idx = self.idx;
        if f & COMPLEX == 0 {
            let own = c.owners.as_ref().map_or(true, |o| o[idx.owner[r] as usize]);
            let cls = c.classes.map_or(true, |m| m >> ((f >> CLS_SHIFT) & 3) & 1 != 0);
            return if own && cls { (idx.size[r], 1) } else { (0, 0) };
        }
        let cx = idx.complex_of(r);
        let (own_sel, own_pass) = match &c.owners {
            None => (idx.size[r], true),
            Some(o) => {
                let sel = cx.owners.iter().filter(|s| o[s.0 as usize]);
                (sel.clone().map(|s| s.1).sum(), sel.count() > 0)
            }
        };
        let (cls_sel, cls_pass) = match c.classes {
            None => (idx.size[r], true),
            Some(m) => ((0..4).filter(|k| m >> k & 1 != 0 && cx.cls_mask >> k & 1 != 0).map(|k| cx.cls[k]).sum(), cx.cls_mask & m != 0),
        };
        let bytes = match (c.owners.is_some(), c.classes.is_some()) {
            (false, false) => idx.size[r],
            (true, false) => own_sel,
            (false, true) => cls_sel,
            (true, true) => {
                if cx.cls_mask.count_ones() == 1 {
                    if cls_pass { own_sel } else { 0 }
                } else if cx.owners.len() == 1 {
                    if own_pass { cls_sel } else { 0 }
                } else {
                    *errors += 1;
                    0
                }
            }
        };
        if own_pass && cls_pass { (bytes, 1) } else { (0, 0) }
    }

    fn chunk<const SEARCH: bool, const DIFF: bool, const FILT: bool>(&self, lo: usize, hi: usize) -> ChunkOut {
        let idx = self.idx;
        let c = self.c;
        let (sa, sb) = (c.sa, c.sb.unwrap_or(0));
        let (dp, maxd) = (c.dp, c.maxd);
        let ppm = c.min_ppm as u128;
        let n_path = c.search.as_ref().map_or(0, |s| s.path_atoms.len());
        let mut mask = [0u32; 257];
        let mut pstate = vec![[0u32; 257]; n_path];
        let mut out = ChunkOut::default();
        let mut frames: Vec<Frame> = Vec::with_capacity(64);
        frames.push(Frame { node: u32::MAX, depth: dp, partial: true, a: Acc::default(), b: Acc::default() });
        for &g in &idx.chain(lo) {
            let g = g as usize;
            let d = idx.depth[g];
            if SEARCH {
                self.atom_bits(g, d as usize, &mut mask, &mut pstate);
            }
            if d > dp && d <= maxd {
                frames.push(Frame { node: g as u32, depth: d, partial: true, a: Acc::default(), b: Acc::default() });
            }
        }
        let lut = c.search.as_ref().map(|s| &s.lut[..]).unwrap_or(&[]);
        let staged = c.staged.as_deref().unwrap_or(&[]);
        let mut sp = staged.partition_point(|iv| (iv.1 as usize) < lo);
        let (mut run_a, mut run_b, mut pub_a, mut pub_b) = (0u64, 0u64, 0u64, 0u64);
        let mut lb: u128 = 0;
        let (depth, flags, end, vf, vt, size) = (&idx.depth[..], &idx.flags[..], &idx.end[..], &idx.vf[..], &idx.vt[..], &idx.size[..]);

        let pop = |frames: &mut Vec<Frame>, out: &mut ChunkOut, lb: u128| {
            let f = frames.pop().unwrap();
            let p = frames.last_mut().unwrap();
            p.a.add(f.a);
            if DIFF {
                p.b.add(f.b);
            }
            if f.a.nonzero() || f.b.nonzero() {
                let n = NodeOut { row: f.node, depth: f.depth, a: f.a, b: f.b };
                if f.partial || end[f.node as usize] as usize >= hi {
                    out.partial.push(n);
                } else if (f.a.bytes.max(f.b.bytes) as u128) * 1_000_000 >= lb {
                    out.done.push(n);
                }
            }
        };

        for r in lo..hi {
            let fl = unsafe { *flags.get_unchecked(r) };
            let d = unsafe { *depth.get_unchecked(r) };
            if fl & CONT == 0 {
                while frames.len() > 1 && frames.last().unwrap().depth >= d {
                    pop(&mut frames, &mut out, lb);
                }
                if SEARCH {
                    self.atom_bits(r, d as usize, &mut mask, &mut pstate);
                }
                if d <= maxd {
                    frames.push(Frame { node: r as u32, depth: d, partial: false, a: Acc::default(), b: Acc::default() });
                }
            }
            if fl & FILE != 0 {
                let (f0, t0) = unsafe { (*vf.get_unchecked(r), *vt.get_unchecked(r)) };
                let la = f0 <= sa && sa < t0;
                let lbv = DIFF && f0 <= sb && sb < t0;
                if la || lbv {
                    if SEARCH && !lut[mask[d as usize] as usize] {
                        continue;
                    }
                    let (bytes, cnt) = if FILT {
                        let ok = if c.staged.is_some() {
                            while sp < staged.len() && (staged[sp].1 as usize) < r {
                                sp += 1;
                            }
                            sp < staged.len() && staged[sp].0 as usize <= r
                        } else {
                            true
                        };
                        let x = self.filtered(r, fl, ok, &mut out.errors);
                        if x.1 == 0 {
                            continue;
                        }
                        x
                    } else {
                        (unsafe { *size.get_unchecked(r) }, 1)
                    };
                    let top = frames.last_mut().unwrap();
                    if la {
                        top.a.bytes += bytes;
                        top.a.files += cnt;
                        run_a += bytes;
                    }
                    if lbv {
                        top.b.bytes += bytes;
                        top.b.files += cnt;
                        run_b += bytes;
                    }
                }
            }
            if r & 0xFFFF == 0 && ppm > 0 {
                let ta = self.ga.fetch_add(run_a - pub_a, Ordering::Relaxed) + run_a - pub_a;
                let tb = if DIFF { self.gb.fetch_add(run_b - pub_b, Ordering::Relaxed) + run_b - pub_b } else { 0 };
                pub_a = run_a;
                pub_b = run_b;
                lb = ppm * ta.max(tb) as u128;
            }
        }
        self.ga.fetch_add(run_a - pub_a, Ordering::Relaxed);
        self.gb.fetch_add(run_b - pub_b, Ordering::Relaxed);
        while frames.len() > 1 {
            pop(&mut frames, &mut out, lb);
        }
        out.a = frames[0].a;
        out.b = frames[0].b;
        out
    }

    fn chunk_any(&self, lo: usize, hi: usize) -> ChunkOut {
        let (s, d, f) = (self.c.search.is_some(), self.c.sb.is_some(), self.c.filtered());
        match (s, d, f) {
            (false, false, false) => self.chunk::<false, false, false>(lo, hi),
            (false, false, true) => self.chunk::<false, false, true>(lo, hi),
            (false, true, false) => self.chunk::<false, true, false>(lo, hi),
            (false, true, true) => self.chunk::<false, true, true>(lo, hi),
            (true, false, false) => self.chunk::<true, false, false>(lo, hi),
            (true, false, true) => self.chunk::<true, false, true>(lo, hi),
            (true, true, false) => self.chunk::<true, true, false>(lo, hi),
            (true, true, true) => self.chunk::<true, true, true>(lo, hi),
        }
    }
}

/// Bytes of the columns a pass reads per row (`depth`, `flags`, `vf`, `vt`, `size`; `name` with a search; `owner`
/// with an owner scope), plus the dictionary bytes the search scanned.
fn bytes_touched(c: &Compiled) -> u64 {
    let rows = (c.hi - c.lo) as u64;
    let mut per = 1 + 1 + 2 + 2 + 8;
    if c.search.is_some() {
        per += 4;
    }
    if c.owners.is_some() {
        per += 2;
    }
    rows * per + c.search.as_ref().map_or(0, |s| s.dict_bytes)
}

/// Answer `q` on `pool`'s threads.
pub fn run(idx: &Index, q: &Query, pool: &rayon::ThreadPool) -> Result<(Answer, Timing)> {
    let t0 = Instant::now();
    let threads = pool.current_num_threads();
    let c = pool.install(|| compile(idx, q, threads))?;
    let compile_ms = t0.elapsed().as_secs_f64() * 1e3;
    if c.dirs {
        return Ok(run_dirs(idx, &c, q, pool, t0, compile_ms));
    }
    let t1 = Instant::now();
    let len = c.hi.saturating_sub(c.lo);
    let step = (len.div_ceil(threads * 8)).max(1 << 16);
    let spans: Vec<(usize, usize)> = (c.lo..c.hi).step_by(step.max(1)).map(|a| (a, (a + step).min(c.hi))).collect();
    let ctx = Ctx { idx, c: &c, ga: AtomicU64::new(0), gb: AtomicU64::new(0) };
    let outs: Vec<ChunkOut> = pool.install(|| spans.par_iter().map(|&(a, b)| ctx.chunk_any(a, b)).collect());
    let pass_ms = t1.elapsed().as_secs_f64() * 1e3;
    let t2 = Instant::now();
    let errors: u64 = outs.iter().map(|o| o.errors).sum();
    if errors > 0 {
        return err(format!("{}: {errors} files hold several owner and several class slices", q.id));
    }
    let answer = assemble(idx, &c, q, outs);
    let assemble_ms = t2.elapsed().as_secs_f64() * 1e3;
    let timing = Timing {
        compile_ms,
        pass_ms,
        assemble_ms,
        total_ms: t0.elapsed().as_secs_f64() * 1e3,
        rows: len as u64,
        bytes: bytes_touched(&c),
        chunks: spans.len() as u64,
    };
    Ok((answer, timing))
}

/// The directory-total view: walk the depth window's paths (descending into a directory only above the deepest level),
/// each node's totals its own row's at the scan; the folder's total its own row's (the fleet root's: its buckets').
fn run_dirs(idx: &Index, c: &Compiled, q: &Query, pool: &rayon::ThreadPool, t0: Instant, compile_ms: f64) -> (Answer, Timing) {
    let t1 = Instant::now();
    let val = |g: usize, s: Option<u16>| -> Acc {
        s.and_then(|s| idx.row_total(g, s)).map_or(Acc::default(), |(bytes, files)| Acc { bytes, files })
    };
    // The view's totals first (the folder's own row; the root's: its buckets'), so nodes under the threshold are never
    // emitted.
    let (ta, tb) = if c.dp > 0 {
        let g = idx.find(&c.path).unwrap();
        (val(g, Some(c.sa)), val(g, c.sb))
    } else {
        let (mut ta, mut tb) = (Acc::default(), Acc::default());
        let mut r = c.lo;
        while r < c.hi {
            ta.add(val(r, Some(c.sa)));
            tb.add(val(r, c.sb));
            r = idx.end[r] as usize + 1;
        }
        (ta, tb)
    };
    let thr = c.min_ppm as u128 * ta.bytes.max(tb.bytes) as u128;
    // The window's nodes are exactly the paths at depth ≤ maxd under the folder, so the folder's rows split into
    // chunks: a chunk steps to its first such path, then jumps over every subtree below the window.
    let threads = pool.current_num_threads();
    let len = c.hi.saturating_sub(c.lo);
    let step = (len.div_ceil(threads * 8)).max(1 << 16);
    let spans: Vec<(usize, usize)> = (c.lo..c.hi).step_by(step.max(1)).map(|a| (a, (a + step).min(c.hi))).collect();
    let outs: Vec<(ChunkOut, u64)> = pool.install(|| {
        spans
            .par_iter()
            .map(|&(lo, hi)| {
                let mut out = ChunkOut::default();
                let mut visited = 0u64;
                let mut r = lo;
                // A chunk starting inside a window node's subtree below the window (or inside its versions) skips
                // past it at once: its ancestor chain says which.
                if lo > c.lo {
                    for &g in &idx.chain(lo) {
                        let g = g as usize;
                        let d = idx.depth[g];
                        if d > c.dp && (d == c.maxd || idx.flags[g] & FILE != 0) {
                            r = idx.end[g] as usize + 1;
                            break;
                        }
                        if d > c.dp && idx.group_end(g) > r {
                            r = idx.group_end(g);
                        }
                    }
                }
                while r < hi {
                    let d = idx.depth[r];
                    if d > c.maxd || idx.flags[r] & CONT != 0 {
                        r += 1;
                        continue;
                    }
                    visited += 1;
                    let (a, b) = (val(r, Some(c.sa)), val(r, c.sb));
                    if (a.nonzero() || b.nonzero()) && (a.bytes.max(b.bytes) as u128) * 1_000_000 >= thr {
                        out.done.push(NodeOut { row: r as u32, depth: d, a, b });
                    }
                    r = if d < c.maxd && idx.flags[r] & FILE == 0 { r + 1 } else { idx.end[r] as usize + 1 };
                }
                (out, visited)
            })
            .collect()
    });
    let visited: u64 = outs.iter().map(|o| o.1).sum();
    let mut outs: Vec<ChunkOut> = outs.into_iter().map(|o| o.0).collect();
    if outs.is_empty() {
        outs.push(ChunkOut::default());
    }
    outs[0].a = ta;
    outs[0].b = tb;
    let pass_ms = t1.elapsed().as_secs_f64() * 1e3;
    let t2 = Instant::now();
    let chunks = outs.len() as u64;
    let answer = assemble(idx, c, q, outs);
    let assemble_ms = t2.elapsed().as_secs_f64() * 1e3;
    let timing = Timing {
        compile_ms,
        pass_ms,
        assemble_ms,
        total_ms: t0.elapsed().as_secs_f64() * 1e3,
        rows: visited,
        bytes: visited * (1 + 1 + 4 + 8 + 2) + 20 * visited,
        chunks,
    };
    (answer, timing)
}

fn assemble(idx: &Index, c: &Compiled, q: &Query, outs: Vec<ChunkOut>) -> Answer {
    let (mut ta, mut tb) = (Acc::default(), Acc::default());
    let mut nodes: Vec<NodeOut> = Vec::new();
    let mut partial: HashMap<u32, NodeOut> = HashMap::new();
    for o in outs {
        ta.add(o.a);
        tb.add(o.b);
        nodes.extend(o.done);
        for p in o.partial {
            partial
                .entry(p.row)
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
    nodes.sort_unstable_by_key(|n| n.row);

    // Paths and remainders: kept nodes in row order; every kept node's ancestors in the window are kept too.
    struct Open {
        depth: u8,
        /// The last row of its subtree.
        last: usize,
        path: String,
        a: Acc,
        b: Acc,
        kids_a: Acc,
        kids_b: Acc,
    }
    let mut out_nodes = Vec::with_capacity(nodes.len());
    let mut rest = Vec::new();
    let mut stack: Vec<Open> = vec![Open { depth: c.dp, last: usize::MAX, path: c.path.clone(), a: ta, b: tb, kids_a: Acc::default(), kids_b: Acc::default() }];
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
        while stack.len() > 1 && (stack.last().unwrap().depth >= n.depth || stack.last().unwrap().last < n.row as usize) {
            let o = stack.pop().unwrap();
            close(o, &mut rest);
        }
        let parent = stack.last_mut().unwrap();
        // With directory totals a child can outweigh its parent (a store whose rows don't add up), so a kept node's
        // parent may not be kept: its rest then isn't charged, and its path comes from the index.
        let path = if parent.depth + 1 == n.depth {
            parent.kids_a.add(n.a);
            parent.kids_b.add(n.b);
            let name = idx.dict.orig(idx.name[n.row as usize]);
            if parent.path.is_empty() { name.to_string() } else { format!("{}/{}", parent.path, name) }
        } else {
            idx.path_of(n.row as usize)
        };
        out_nodes.push((path.clone(), n.a.bytes, n.a.files, n.b.bytes, n.b.files));
        stack.push(Open { depth: n.depth, last: idx.end[n.row as usize] as usize, path, a: n.a, b: n.b, kids_a: Acc::default(), kids_b: Acc::default() });
    }
    while let Some(o) = stack.pop() {
        close(o, &mut rest);
    }
    out_nodes.sort();
    rest.sort();
    Answer { id: q.id.clone(), total: [ta.bytes, ta.files, tb.bytes, tb.files], nodes: out_nodes, rest }
}
