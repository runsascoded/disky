//! Reading a local scan's store: the site's `/api/subtree` and `/api/series`
//! answers (`site/functions/_lib/view.ts` `buildView`, `api/series.ts`), for
//! the unscoped view a laptop serves (no owner lens, pools, class or name
//! filters).
//!
//! A view of P at a `w`×`h` canvas keeps every path under P whose bytes clear
//! `thr · atten^(depth − dP − 1)`, `thr = P.b · minArea / (w·h)`, nests them
//! (children by bytes, descending) and closes each parent with `(other)` =
//! parent − Σ kept children when that clears the children's threshold. The
//! rows come from whichever sort decodes fewer row groups: `path` (P's
//! `(depth, path)` ranges) or `bysize` (the groups whose sizes can clear the
//! smallest threshold), using the footer's statistics.

use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::cast::AsArray;
use arrow_array::types::{Float64Type, Int32Type, Int64Type};
use arrow_array::{Array, Float64Array, Int32Array, Int64Array, RecordBatch, StringArray};
use parquet::arrow::arrow_reader::{ArrowReaderMetadata, ParquetRecordBatchReaderBuilder};
use parquet::arrow::ProjectionMask;
use parquet::file::statistics::Statistics;
use serde_json::{json, Map, Value};

use crate::query::Query;
use crate::{BYSIZE_FILE, N_AGE, PATH_FILE};

/// Response nodes, at most (`view.ts` `HARD_CAP`).
const HARD_CAP: usize = 50_000;

fn err<E: std::fmt::Display>(e: E) -> io::Error {
    io::Error::other(e.to_string())
}

/// One decoded store row (the columns a view reads).
#[derive(Clone, Debug)]
pub struct VRow {
    pub path: String,
    pub depth: i32,
    pub dir: bool,
    pub size: i64,
    pub n_files: i64,
    pub n_children: i64,
    pub mtime_mean: Option<f64>,
    pub ages: [i64; N_AGE],
}

#[derive(Clone)]
struct Group {
    rows: usize,
    d: (i32, i32),
    /// Path bounds (byte strings; absent when the writer kept none).
    p: Option<(Vec<u8>, Vec<u8>)>,
    s: (i64, i64),
}

/// One sort of a scan: its footer and per-group bounds.
struct Tier {
    file: PathBuf,
    meta: ArrowReaderMetadata,
    groups: Vec<Group>,
}

const COLS: [&str; 8] = ["path", "depth", "kind", "size", "n_files", "n_children", "mtime_mean", "age_b0"];

impl Tier {
    fn open(file: PathBuf) -> io::Result<Tier> {
        let meta = ArrowReaderMetadata::load(&File::open(&file)?, Default::default()).map_err(err)?;
        let schema = meta.parquet_schema();
        let col = |n: &str| (0..schema.num_columns()).find(|&i| schema.column(i).name() == n).ok_or_else(|| err(format!("{}: no `{n}`", file.display())));
        let (ip, id, is) = (col("path")?, col("depth")?, col("size")?);
        let groups = meta
            .metadata()
            .row_groups()
            .iter()
            .map(|g| {
                let i32s = |i: usize| match g.column(i).statistics() {
                    Some(Statistics::Int32(s)) => (s.min_opt().copied().unwrap_or(i32::MIN), s.max_opt().copied().unwrap_or(i32::MAX)),
                    _ => (i32::MIN, i32::MAX),
                };
                let i64s = |i: usize| match g.column(i).statistics() {
                    Some(Statistics::Int64(s)) => (s.min_opt().copied().unwrap_or(i64::MIN), s.max_opt().copied().unwrap_or(i64::MAX)),
                    _ => (i64::MIN, i64::MAX),
                };
                let p = match g.column(ip).statistics() {
                    Some(Statistics::ByteArray(s)) if s.min_is_exact() && s.max_is_exact() => {
                        s.min_opt().zip(s.max_opt()).map(|(a, b)| (a.data().to_vec(), b.data().to_vec()))
                    }
                    _ => None,
                };
                Group { rows: g.num_rows() as usize, d: i32s(id), p, s: i64s(is) }
            })
            .collect();
        Ok(Tier { file, meta, groups })
    }

    fn read(&self, groups: Vec<usize>) -> io::Result<Vec<VRow>> {
        let mut out = vec![];
        self.scan(groups, true, |c| {
            out.extend((0..c.len()).map(|i| c.row(i)));
            Ok(())
        })?;
        Ok(out)
    }

    /// Each decoded batch of `groups`, in file order; `full: false` decodes
    /// only `path`, `depth` and `size`.
    fn scan(&self, groups: Vec<usize>, full: bool, mut f: impl FnMut(&Cols) -> io::Result<()>) -> io::Result<()> {
        if groups.is_empty() {
            return Ok(());
        }
        let schema = self.meta.parquet_schema();
        let leaves: Vec<usize> = (0..schema.num_columns())
            .filter(|&i| {
                let n = schema.column(i).name().to_string();
                if full { COLS.contains(&n.as_str()) || n.starts_with("age_b") } else { LITE.contains(&n.as_str()) }
            })
            .collect();
        let reader = ParquetRecordBatchReaderBuilder::new_with_metadata(File::open(&self.file)?, self.meta.clone())
            .with_row_groups(groups)
            .with_projection(ProjectionMask::leaves(schema, leaves))
            .with_batch_size(8192)
            .build()
            .map_err(err)?;
        for b in reader {
            f(&Cols::of(&b.map_err(err)?)?)?;
        }
        Ok(())
    }

    /// `f` over each of `groups` on a thread pool (one reader per group);
    /// results in `groups` order.
    fn par_groups<T: Send>(&self, groups: Vec<usize>, f: impl Fn(usize) -> io::Result<T> + Sync) -> io::Result<Vec<T>> {
        let n = std::thread::available_parallelism().map_or(4, |n| n.get()).min(groups.len().max(1));
        let next = std::sync::atomic::AtomicUsize::new(0);
        let mut out: Vec<(usize, T)> = std::thread::scope(|s| {
            let hs: Vec<_> = (0..n)
                .map(|_| {
                    s.spawn(|| -> io::Result<Vec<(usize, T)>> {
                        let mut got = vec![];
                        loop {
                            let i = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                            let Some(&g) = groups.get(i) else { break };
                            got.push((i, f(g)?));
                        }
                        Ok(got)
                    })
                })
                .collect();
            let mut all = vec![];
            for h in hs {
                all.extend(h.join().map_err(|_| err("reader thread panicked"))??);
            }
            Ok::<_, io::Error>(all)
        })?;
        out.sort_by_key(|x| x.0);
        Ok(out.into_iter().map(|x| x.1).collect())
    }
}

/// The columns a filter's first pass reads.
const LITE: [&str; 3] = ["path", "depth", "size"];

/// A batch's columns, typed (`None`: not projected).
struct Cols {
    path: StringArray,
    depth: Int32Array,
    size: Int64Array,
    rest: Option<Rest>,
}

struct Rest {
    kind: StringArray,
    n_files: Int64Array,
    n_children: Int64Array,
    mean: Float64Array,
    ages: Vec<Option<Int64Array>>,
}

impl Cols {
    fn of(b: &RecordBatch) -> io::Result<Cols> {
        let col = |n: &str| b.column_by_name(n).ok_or_else(|| err(format!("no `{n}` column")));
        let rest = match b.column_by_name("kind") {
            None => None,
            Some(_) => Some(Rest {
                kind: col("kind")?.as_string::<i32>().clone(),
                n_files: col("n_files")?.as_primitive::<Int64Type>().clone(),
                n_children: col("n_children")?.as_primitive::<Int64Type>().clone(),
                mean: col("mtime_mean")?.as_primitive::<Float64Type>().clone(),
                ages: (0..N_AGE).map(|i| b.column_by_name(&format!("age_b{i}")).map(|c| c.as_primitive::<Int64Type>().clone())).collect(),
            }),
        };
        Ok(Cols {
            path: col("path")?.as_string::<i32>().clone(),
            depth: col("depth")?.as_primitive::<Int32Type>().clone(),
            size: col("size")?.as_primitive::<Int64Type>().clone(),
            rest,
        })
    }

    fn len(&self) -> usize {
        self.path.len()
    }

    /// Row `i` (a full projection's).
    fn row(&self, i: usize) -> VRow {
        let r = self.rest.as_ref().expect("a full projection");
        let mut a = [0; N_AGE];
        for (j, c) in r.ages.iter().enumerate() {
            if let Some(c) = c {
                a[j] = c.value(i);
            }
        }
        VRow {
            path: self.path.value(i).to_string(),
            depth: self.depth.value(i),
            dir: r.kind.value(i) == "dir",
            size: self.size.value(i),
            n_files: r.n_files.value(i),
            n_children: r.n_children.value(i),
            mtime_mean: (!r.mean.is_null(i)).then(|| r.mean.value(i)),
            ages: a,
        }
    }
}

/// The row range `[lo, hi)` at depths `d` of a `(depth, path)`-sorted tier.
#[derive(Clone)]
struct Rect {
    d: (i32, i32),
    lo: String,
    /// `None`: no upper bound (the store root's range).
    hi: Option<String>,
}

impl Rect {
    /// Exactly `path`'s own row(s) (every depth-1 row for the root).
    fn at(path: &str) -> Rect {
        if path.is_empty() {
            Rect { d: (1, 1), lo: String::new(), hi: None }
        } else {
            let d = depth_of(path);
            Rect { d: (d, d), lo: path.into(), hi: Some(format!("{path}\0")) }
        }
    }
}

impl Group {
    /// Whether this `path`-sort group can hold a row in `r`.
    fn meets(&self, r: &Rect) -> bool {
        if self.d.1 < r.d.0 || self.d.0 > r.d.1 {
            return false;
        }
        match (&self.p, self.d.0 == self.d.1) {
            // One depth: its paths are a contiguous range.
            (Some((lo, hi)), true) => hi.as_slice() >= r.lo.as_bytes() && r.hi.as_ref().is_none_or(|h| lo.as_slice() < h.as_bytes()),
            _ => true,
        }
    }
}

/// A scan's two sorts.
pub struct Scan {
    path: Tier,
    bysize: Tier,
}

impl Scan {
    pub fn open(dir: &Path) -> io::Result<Scan> {
        Ok(Scan { path: Tier::open(dir.join(PATH_FILE))?, bysize: Tier::open(dir.join(BYSIZE_FILE))? })
    }

    /// Rows of the `path` sort in `r`.
    fn rect(&self, r: &Rect) -> io::Result<Vec<VRow>> {
        let gs = (0..self.path.groups.len()).filter(|&i| self.path.groups[i].meets(r)).collect();
        Ok(self.path.read(gs)?.into_iter().filter(|x| in_rect(x, r)).collect())
    }

    /// Rows in `r` whose size can clear `min`, from the cheaper sort.
    fn subtree(&self, r: &Rect, min: f64) -> io::Result<(Vec<VRow>, &'static str)> {
        let by_path: Vec<usize> = (0..self.path.groups.len()).filter(|&i| self.path.groups[i].meets(r)).collect();
        let by_size: Vec<usize> = (0..self.bysize.groups.len()).filter(|&i| self.bysize.groups[i].s.1 as f64 >= min).collect();
        let rows = |t: &Tier, gs: &[usize]| gs.iter().map(|&i| t.groups[i].rows).sum::<usize>();
        let (rows, tier) = if rows(&self.bysize, &by_size) < rows(&self.path, &by_path) {
            (self.bysize.read(by_size)?, "bysize")
        } else {
            (self.path.read(by_path)?, "path")
        };
        Ok((rows.into_iter().filter(|x| in_rect(x, r) && x.size as f64 >= min).collect(), tier))
    }
}

fn in_rect(x: &VRow, r: &Rect) -> bool {
    in_rect_raw(&x.path, x.depth, r)
}

fn in_rect_raw(p: &str, d: i32, r: &Rect) -> bool {
    d >= r.d.0 && d <= r.d.1 && p >= r.lo.as_str() && r.hi.as_ref().is_none_or(|h| p < h.as_str())
}

/// A path's aggregate (`view.ts` `Agg`, unscoped).
#[derive(Clone, Default)]
struct Agg {
    b: f64,
    o: f64,
    wts: f64,
    wb: f64,
    dir: Option<bool>,
    nc: Option<i64>,
    ag: Option<[f64; N_AGE]>,
}

impl Agg {
    fn merge(&mut self, r: &VRow) {
        self.b += r.size as f64;
        self.o += r.n_files as f64;
        if let Some(m) = r.mtime_mean {
            if r.size > 0 {
                self.wts += m * r.size as f64;
                self.wb += r.size as f64;
            }
        }
        self.dir = Some(r.dir);
        self.nc = Some(r.n_children);
        let ag = self.ag.get_or_insert([0.0; N_AGE]);
        for (a, v) in ag.iter_mut().zip(r.ages) {
            *a += v as f64;
        }
    }

    fn subtract(&self, kids: &[&Agg]) -> Agg {
        let sum = |f: &dyn Fn(&Agg) -> f64| kids.iter().map(|k| f(k)).sum::<f64>();
        Agg {
            b: self.b - sum(&|a| a.b),
            o: (self.o - sum(&|a| a.o)).max(0.0),
            wts: self.wts - sum(&|a| a.wts),
            wb: (self.wb - sum(&|a| a.wb)).max(0.0),
            dir: None,
            nc: None,
            ag: self.ag.map(|ag| {
                let mut out = [0.0; N_AGE];
                for (i, o) in out.iter_mut().enumerate() {
                    *o = (ag[i] - kids.iter().map(|k| k.ag.map_or(0.0, |a| a[i])).sum::<f64>()).max(0.0);
                }
                out
            }),
        }
    }

    /// `view.ts` `sumAgg`: totals only (kind / child count stay).
    fn add(&mut self, a: &Agg) {
        self.b += a.b;
        self.o += a.o;
        self.wts += a.wts;
        self.wb += a.wb;
        if let Some(x) = a.ag {
            let ag = self.ag.get_or_insert([0.0; N_AGE]);
            for (v, w) in ag.iter_mut().zip(x) {
                *v += w;
            }
        }
    }

    /// `view.ts` `minus`: less the excluded paths below, keeping what the row
    /// says the path is.
    fn minus(&self, cut: Option<&Agg>, lost_kids: i64) -> Agg {
        let mut out = match cut {
            Some(c) => self.subtract(&[c]),
            None => self.clone(),
        };
        out.dir = self.dir;
        out.nc = self.nc.map(|n| (n - lost_kids).max(0));
        out
    }

    /// The wire node: `{n, k, b, o[, d][, ag]}` (`nodeOf` + `display`).
    fn node(&self, name: &str) -> Map<String, Value> {
        let mut m = Map::new();
        m.insert("n".into(), name.into());
        m.insert("k".into(), if self.dir == Some(false) { "file" } else { "dir" }.into());
        m.insert("b".into(), js_round(self.b).into());
        m.insert("o".into(), js_round(self.o).into());
        if self.wb != 0.0 {
            m.insert("d".into(), js_round(self.wts / self.wb / 86400.0).into());
        }
        if let Some(ag) = self.ag.filter(|ag| ag.iter().any(|&v| v > 0.0)) {
            m.insert("ag".into(), ag.iter().map(|&v| js_round(v)).collect::<Vec<_>>().into());
        }
        m
    }
}

/// `Math.round`: halves toward +∞.
fn js_round(x: f64) -> i64 {
    (x + 0.5).floor() as i64
}

fn parent_of(p: &str) -> &str {
    p.rfind('/').map_or("", |i| &p[..i])
}

fn depth_of(p: &str) -> i32 {
    if p.is_empty() { 0 } else { p.split('/').count() as i32 }
}

/// `[lo, hi)` of the paths strictly under `path` (`'0'` sorts just past `'/'`).
fn under(path: &str) -> (String, Option<String>) {
    if path.is_empty() { (String::new(), None) } else { (format!("{path}/"), Some(format!("{path}0"))) }
}

pub struct ViewOpts<'a> {
    pub path: &'a str,
    pub w: f64,
    pub h: f64,
    pub min_area: f64,
    pub atten: f64,
    pub max_depth: Option<i32>,
    /// The root's name when `path` is the store root.
    pub root_label: &'a str,
    /// `q=`: the name filter.
    pub query: Option<&'a Query>,
}

/// `/api/subtree`'s body fields: `{tier, index, threshold, nodes, truncated, tree}`.
pub fn subtree(scan: &Scan, o: &ViewOpts) -> io::Result<Option<Map<String, Value>>> {
    let path = o.path;
    let dp = depth_of(path);
    let root_rows = scan.rect(&Rect::at(path))?;
    if root_rows.is_empty() {
        return Ok(None);
    }
    let mut root = Agg::default();
    for r in &root_rows {
        root.merge(r);
    }
    if path.is_empty() {
        root.nc = Some(root_rows.len() as i64);
        root.dir = Some(true);
    }
    let root_name = if path.is_empty() { o.root_label.to_string() } else { path.rsplit('/').next().unwrap().to_string() };
    let mut body = Map::new();
    if root.b <= 0.0 {
        let mut n = Map::new();
        n.insert("n".into(), root_name.into());
        n.insert("k".into(), "dir".into());
        n.insert("b".into(), 0.into());
        n.insert("o".into(), 0.into());
        body.extend([("tier".into(), "none".into()), ("index".into(), "none".into()), ("threshold".into(), 0.into()), ("nodes".into(), 0.into()), ("truncated".into(), false.into())]);
        if o.query.is_some() {
            body.extend([("matches".into(), json!([])), ("matched".into(), json!([]))]);
        }
        body.insert("tree".into(), n.into());
        return Ok(Some(body));
    }
    // A filter view, unless the root itself matches with nothing to exclude
    // (then the plain view is the answer, the root its one match).
    let root_hit = o.query.is_some_and(|q| q.matches(path));
    if let Some(q) = o.query.filter(|q| !root_hit || q.has_neg()) {
        return filtered(scan, o, q, &root, &root_name).map(Some);
    }
    let threshold = root.b * o.min_area / (o.w * o.h);
    let thr_at = |d: i32| threshold * o.atten.powi((d - dp - 1).max(0));
    let (lo, hi) = under(path);
    let d_hi = o.max_depth.map_or(i32::MAX, |m| dp + m);
    let (rows, tier) = scan.subtree(&Rect { d: (dp + 1, d_hi), lo, hi }, threshold)?;
    let mut aggs: HashMap<String, (i32, Agg)> = HashMap::new();
    let mut order: Vec<String> = vec![];
    for r in rows {
        if (r.size as f64) < thr_at(r.depth) {
            continue;
        }
        let e = aggs.entry(r.path.clone()).or_insert_with(|| {
            order.push(r.path.clone());
            (r.depth, Agg::default())
        });
        e.1.merge(&r);
    }
    order.retain(|p| aggs[p].1.b > 0.0);
    let truncated = order.len() > HARD_CAP;
    if truncated {
        order.sort_by(|a, b| aggs[b].1.b.total_cmp(&aggs[a].1.b));
        order.truncate(HARD_CAP);
    }
    // Rows arrive in the sort's order; nest them in `(depth, path)` order so
    // equal-byte siblings keep it (the site's stable sort over its read order).
    order.sort_by(|a, b| aggs[a].0.cmp(&aggs[b].0).then_with(|| a.cmp(b)));
    let kept: HashSet<&str> = order.iter().map(String::as_str).collect();
    let kids = kids_index(&order, &kept, path);
    let marked: HashSet<String> = if root_hit { [path.to_string()].into() } else { HashSet::new() };
    let tree = build(path, &root, &root_name, &aggs, &kids, &thr_at, &HashMap::new(), &marked);
    body.extend([
        ("tier".into(), tier.into()),
        ("index".into(), "footer".into()),
        ("threshold".into(), js_round(threshold).into()),
        ("nodes".into(), order.len().into()),
        ("truncated".into(), truncated.into()),
    ]);
    if root_hit {
        body.extend([("matches".into(), json!([path])), ("matched".into(), json!([]))]);
    }
    body.insert("tree".into(), tree);
    Ok(Some(body))
}

/// The view tree under `p` (`buildView`'s `build`): children by bytes, then
/// `(other)` = parent − Σ kept children when it clears their threshold; its
/// `f` is the parent's unnamed children (`nc`), else the folded ones counted.
#[allow(clippy::too_many_arguments)]
fn build(p: &str, a: &Agg, name: &str, aggs: &HashMap<String, (i32, Agg)>, kids: &HashMap<&str, Vec<&str>>, thr_at: &dyn Fn(i32) -> f64, folded: &HashMap<String, i64>, marked: &HashSet<String>) -> Value {
    let mut node = a.node(name);
    if marked.contains(p) {
        node.insert("m".into(), 1.into());
    }
    let Some(cs) = kids.get(p) else { return node.into() };
    let mut children: Vec<(f64, Value)> = cs
        .iter()
        .map(|c| {
            let (_, ca) = &aggs[*c];
            (js_round(ca.b) as f64, build(c, ca, c.rsplit('/').next().unwrap(), aggs, kids, thr_at, folded, marked))
        })
        .collect();
    children.sort_by(|x, y| y.0.total_cmp(&x.0));
    let kid_aggs: Vec<&Agg> = cs.iter().map(|c| &aggs[*c].1).collect();
    let rest = a.subtract(&kid_aggs);
    let mut c: Vec<Value> = children.into_iter().map(|(_, v)| v).collect();
    if rest.b > thr_at(aggs[cs[0]].0) {
        let mut other = rest.node("(other)");
        let f = match a.nc {
            Some(nc) => (nc - cs.len() as i64).max(0),
            None => folded.get(p).copied().unwrap_or(0),
        };
        other.insert("f".into(), f.into());
        c.push(other.into());
    }
    node.insert("c".into(), c.into());
    node.into()
}

/// Kept paths by parent (`kidsIndex`): a path whose parent isn't kept hangs
/// off the view root.
fn kids_index<'a>(order: &'a [String], kept: &HashSet<&str>, root: &'a str) -> HashMap<&'a str, Vec<&'a str>> {
    let mut kids: HashMap<&str, Vec<&str>> = HashMap::new();
    for p in order {
        let par = parent_of(p);
        let key = if kept.contains(par) { par } else { root };
        kids.entry(key).or_default().push(p);
    }
    kids
}

/// A set of paths, and the depths they sit at: the member above a row is
/// found by probing only those depths' prefixes.
#[derive(Default)]
struct PathSet {
    set: HashSet<String>,
    depths: Vec<i32>,
}

impl PathSet {
    fn insert(&mut self, p: String) {
        let d = depth_of(&p);
        if !self.depths.contains(&d) {
            self.depths.push(d);
            self.depths.sort();
        }
        self.set.insert(p);
    }

    fn contains(&self, p: &str) -> bool {
        self.set.contains(p)
    }

    /// The member strictly above `p` (at depth `d`).
    fn above<'a>(&self, p: &'a str, d: i32) -> Option<&'a str> {
        for &k in &self.depths {
            if k >= d {
                break;
            }
            let pre = if k == 0 { "" } else { p.match_indices('/').nth(k as usize - 1).map_or(p, |(i, _)| &p[..i]) };
            if self.set.contains(pre) {
                return Some(pre);
            }
        }
        None
    }
}

/// A filtered view (`q=`; `readView`'s filter branch, specs/filter-views.md
/// §2), exact: every row under P is read, so the match roots, exclusions and
/// totals are the whole scan's (the site's search index and read budgets
/// approximate these on big stores). A match root is an outermost path under
/// P the query holds; an exclusion (NOT) is an outermost path under a match
/// root the negative part holds, its bytes subtracted up to the root. The
/// forest keeps each root's descendants that clear one threshold — the
/// matched total's pixel threshold, attenuated from the root's own depth.
fn filtered(scan: &Scan, o: &ViewOpts, q: &Query, root_all: &Agg, root_name: &str) -> io::Result<Map<String, Value>> {
    let path = o.path;
    let dp = depth_of(path);
    let (lo, hi) = under(path);
    let rect = Rect { d: (dp + 1, i32::MAX), lo, hi };
    let groups: Vec<usize> = (0..scan.path.groups.len()).filter(|&i| scan.path.groups[i].meets(&rect)).collect();
    let root_hit = q.matches(path);
    let mono = q.monotone();
    // Pass 1 (paths only, parallel): the rows the query holds and, with NOT,
    // the rows its negative part holds. Substring / glob tests are monotone
    // along a path (a match's descendants match), so only each chain's top
    // is a candidate; a regex's every match is.
    struct Hit {
        path: String,
        depth: i32,
        size: i64,
        neg: bool,
    }
    let hits = scan.path.par_groups(groups.clone(), |g| {
        let mut out = vec![];
        scan.path.scan(vec![g], false, |c| {
            for i in 0..c.len() {
                let (p, d) = (c.path.value(i), c.depth.value(i));
                if !in_rect_raw(p, d, &rect) {
                    continue;
                }
                // The parent is strictly under P (a pruned chain's top is then a row too).
                let par = (d > dp + 1).then(|| parent_of(p));
                let neg = q.has_neg() && q.neg(p);
                let hit = if neg {
                    !(mono && par.is_some_and(|x| q.neg(x)))
                } else {
                    !root_hit && q.pos(p) && !(mono && par.is_some_and(|x| q.pos(x)))
                };
                if hit {
                    out.push(Hit { path: p.into(), depth: d, size: c.size.value(i), neg });
                }
            }
            Ok(())
        })?;
        Ok(out)
    })?;
    let mut hits: Vec<Hit> = hits.into_iter().flatten().collect();
    hits.sort_by(|x, y| x.depth.cmp(&y.depth).then_with(|| x.path.cmp(&y.path)));
    // Outermost, in `(depth, path)` order: every ancestor precedes its descendants.
    let mut roots = PathSet::default();
    let mut excl = PathSet::default();
    let mut bytes: HashMap<String, i64> = HashMap::new();
    if root_hit {
        roots.insert(path.into());
    }
    for h in hits {
        let in_root = roots.above(&h.path, h.depth).is_some();
        if h.neg {
            if in_root && excl.above(&h.path, h.depth).is_none() {
                *bytes.entry(h.path.clone()).or_default() += h.size;
                excl.insert(h.path);
            }
        } else if !in_root {
            *bytes.entry(h.path.clone()).or_default() += h.size;
            roots.insert(h.path);
        }
    }
    let mut body = Map::new();
    let empty = |body: &mut Map<String, Value>| {
        let mut n = Map::new();
        n.insert("n".into(), root_name.into());
        n.insert("k".into(), "dir".into());
        n.insert("b".into(), 0.into());
        n.insert("o".into(), 0.into());
        body.extend([("tier".into(), "none".into()), ("index".into(), "none".into()), ("threshold".into(), 0.into()), ("nodes".into(), 0.into()), ("truncated".into(), false.into()), ("matches".into(), json!([])), ("matched".into(), json!([])), ("tree".into(), n.into())]);
    };
    let matched_b = roots.set.iter().map(|r| if r == path { root_all.b } else { bytes[r] as f64 }).sum::<f64>() - excl.set.iter().map(|e| bytes[e] as f64).sum::<f64>();
    if roots.set.is_empty() || matched_b <= 0.0 {
        empty(&mut body);
        return Ok(body);
    }
    let thr = matched_b * o.min_area / (o.w * o.h);
    let rebased = |root_depth: i32, d: i32| thr * o.atten.powi((d - root_depth - 1).max(0));
    // A root is a tile when its net bytes clear the view's threshold at its
    // depth (attenuated from P, like the plain view); the rest fold into
    // their parent's `(other)`, and their subtrees aren't read. (The site
    // draws every root it found; its reads bound how many that is. Read
    // exactly, a regex like `\.py$` holds ~300K roots.)
    let thr_p = |d: i32| rebased(dp, d);
    let mut root_net: HashMap<&str, i64> = roots.set.iter().map(|r| (r.as_str(), if r == path { root_all.b as i64 } else { bytes[r] })).collect();
    for e in &excl.set {
        if let Some(r) = roots.above(e, depth_of(e)) {
            *root_net.get_mut(r).unwrap() -= bytes[e];
        }
    }
    let mut tiles = PathSet::default();
    for (r, b) in &root_net {
        if *r == path || *b as f64 >= thr_p(depth_of(r)) {
            tiles.insert(r.to_string());
        }
    }
    let deepest = tiles.set.iter().map(|r| depth_of(r)).max().unwrap_or(0);
    let loose = |d: i32| rebased(deepest, d);
    let d_cap = o.max_depth;
    // Pass 2 (parallel): the roots' and exclusions' own rows, and each
    // root's descendants that can clear its threshold (net ≤ raw bytes, and
    // `loose` is the most permissive root's) — a group's full columns are
    // decoded only when it holds one.
    let wanted = |p: &str, d: i32, size: i64| -> bool {
        if roots.contains(p) || excl.contains(p) {
            return true;
        }
        if (size as f64) < loose(d) || d_cap.is_some_and(|m| m <= 0) {
            return false;
        }
        if excl.above(p, d).is_some() {
            return false;
        }
        match tiles.above(p, d) {
            Some(r) => !d_cap.is_some_and(|m| d > depth_of(r) + m),
            None => false,
        }
    };
    // A group is read when it can hold a root's or exclusion's own row, or a
    // root's descendant big enough to keep.
    // (Per-root rects only while they're few: each is tested on every group.)
    const RECTS: usize = 2000;
    let own: Option<Vec<Rect>> = (roots.set.len() + excl.set.len() <= RECTS).then(|| roots.set.iter().chain(&excl.set).filter(|x| x.as_str() != path).map(|x| Rect::at(x)).collect());
    let subs: Option<Vec<Rect>> = (tiles.set.len() <= RECTS).then(|| tiles
        .set
        .iter()
        .map(|r| {
            let (lo, hi) = under(r);
            Rect { d: (depth_of(r) + 1, i32::MAX), lo, hi }
        })
        .collect());
    let live: Vec<usize> = groups
        .into_iter()
        .filter(|&g| {
            let gr = &scan.path.groups[g];
            let meets = |rs: &Option<Vec<Rect>>| rs.as_ref().is_none_or(|rs| rs.iter().any(|r| gr.meets(r)));
            meets(&own) || ((gr.s.1 as f64) >= loose(gr.d.0).min(loose(gr.d.1)) && meets(&subs))
        })
        .collect();
    let got = scan.path.par_groups(live, |g| {
        let mut idx = vec![];
        let mut n = 0;
        scan.path.scan(vec![g], false, |c| {
            for i in 0..c.len() {
                let (p, d) = (c.path.value(i), c.depth.value(i));
                if in_rect_raw(p, d, &rect) && wanted(p, d, c.size.value(i)) {
                    idx.push(n + i);
                }
            }
            n += c.len();
            Ok(())
        })?;
        let mut rows = vec![];
        if idx.is_empty() {
            return Ok(rows);
        }
        let (mut n, mut k) = (0, 0);
        scan.path.scan(vec![g], true, |c| {
            while k < idx.len() && idx[k] < n + c.len() {
                rows.push(c.row(idx[k] - n));
                k += 1;
            }
            n += c.len();
            Ok(())
        })?;
        Ok(rows)
    })?;
    let mut aggs1: HashMap<String, (i32, Agg)> = HashMap::new();
    if root_hit {
        aggs1.insert(path.into(), (dp, root_all.clone()));
    }
    let mut rows: HashMap<String, (i32, Agg)> = HashMap::new();
    for r in got.into_iter().flatten() {
        let into = if roots.contains(&r.path) || excl.contains(&r.path) { &mut aggs1 } else { &mut rows };
        into.entry(r.path.clone()).or_insert((r.depth, Agg::default())).1.merge(&r);
    }
    let root_of = |p: &str| -> String {
        if roots.contains(p) { p.to_string() } else { roots.above(p, depth_of(p)).unwrap().to_string() }
    };
    // Σ excluded aggregates under each path (up to its root), and the
    // excluded direct children per parent.
    let mut cut: HashMap<String, Agg> = HashMap::new();
    let mut lost: HashMap<String, i64> = HashMap::new();
    for e in &excl.set {
        let r = root_of(parent_of(e));
        *lost.entry(parent_of(e).into()).or_default() += 1;
        let a = &aggs1[e].1;
        let mut q2 = parent_of(e);
        loop {
            cut.entry(q2.into()).or_default().add(a);
            if q2 == r {
                break;
            }
            q2 = parent_of(q2);
        }
    }
    let net = |p: &str, a: &Agg| a.minus(cut.get(p), lost.get(p).copied().unwrap_or(0));
    let mut aggs: HashMap<String, (i32, Agg)> = HashMap::new();
    let mut matched = Agg::default();
    let mut matched_list = vec![];
    let mut below: Vec<String> = vec![];
    for r in &roots.set {
        let (d, a) = &aggs1[r];
        let a = net(r, a);
        matched.add(&a);
        matched_list.push((r.clone(), js_round(a.b), js_round(a.o)));
        if r == path {
            continue;
        }
        let mut q2 = parent_of(r);
        while q2.len() > path.len() {
            aggs.entry(q2.into()).or_insert_with(|| (depth_of(q2), Agg { dir: Some(true), ..Default::default() })).1.add(&a);
            if q2.is_empty() {
                break;
            }
            q2 = parent_of(q2);
        }
        if tiles.contains(r) {
            aggs.insert(r.clone(), (*d, a));
        } else {
            below.push(r.clone());
        }
    }
    // A root's ancestors (P excluded) whose matched bytes miss the threshold.
    let thin: Vec<String> = aggs.iter().filter(|(p, (d, a))| !roots.contains(p) && a.b < thr_p(*d)).map(|(p, _)| p.clone()).collect();
    for p in thin {
        aggs.remove(&p);
        below.push(p);
    }
    for (p, (d, a)) in rows {
        let r = root_of(&p);
        let a = net(&p, &a);
        if a.b <= 0.0 {
            continue;
        }
        if a.b >= rebased(aggs1[&r].0, d) {
            aggs.insert(p, (d, a));
        } else {
            below.push(p);
        }
    }
    let mut folded: HashMap<String, i64> = HashMap::new();
    for p in &below {
        let par = parent_of(p);
        if aggs.contains_key(par) {
            *folded.entry(par.into()).or_default() += 1;
        }
    }
    let mut order: Vec<String> = aggs.keys().cloned().collect();
    order.sort_by(|a, b| aggs[a].0.cmp(&aggs[b].0).then_with(|| a.cmp(b)));
    let kept: HashSet<&str> = order.iter().map(String::as_str).collect();
    let kids = kids_index(&order, &kept, path);
    let mut root_agg = matched;
    root_agg.dir = Some(true);
    let tree = build(path, &root_agg, root_name, &aggs, &kids, &loose, &folded, &roots.set);
    matched_list.sort_by(|x, y| y.1.cmp(&x.1).then_with(|| x.0.cmp(&y.0)));
    matched_list.truncate(HARD_CAP);
    let mut matches: Vec<&String> = matched_list.iter().map(|x| &x.0).collect();
    matches.sort();
    body.extend([
        ("tier".into(), "scan+path".into()),
        ("index".into(), "footer".into()),
        ("threshold".into(), js_round(thr).into()),
        ("nodes".into(), order.len().into()),
        ("truncated".into(), (roots.set.len() > HARD_CAP).into()),
        ("matches".into(), json!(matches)),
        ("matched".into(), matched_list.iter().map(|(p, b, o)| json!({"path": p, "b": b, "o": o})).collect::<Vec<_>>().into()),
    ]);
    if !excl.set.is_empty() {
        let mut e: Vec<&String> = excl.set.iter().collect();
        e.sort();
        body.insert("excluded".into(), json!(e));
    }
    body.insert("tree".into(), tree);
    Ok(body)
}

/// P's `{b, o}` in one scan (a `/api/series` point); `None` when P isn't in it.
pub fn point(scan: &Scan, path: &str) -> io::Result<Option<(i64, i64)>> {
    let rows = scan.rect(&Rect::at(path))?;
    Ok((!rows.is_empty()).then(|| rows.iter().fold((0, 0), |(b, o), r| (b + r.size, o + r.n_files))))
}

/// The store's depth-1 rows `{path, b, o}` (`split=roots`).
pub fn roots(scan: &Scan) -> io::Result<Vec<(String, i64, i64)>> {
    Ok(scan.rect(&Rect::at(""))?.into_iter().map(|r| (r.path, r.size, r.n_files)).collect())
}

/// A scans dir: `<dir>/<scan id>/{path-index*.parquet, meta.json, age.json}`.
pub struct Scans {
    pub dir: PathBuf,
    open: std::sync::Mutex<HashMap<String, Arc<Scan>>>,
}

/// `YYYY-MM-DD` or `YYYY-MM-DDTHHMM` (the site's scan-id grammar).
pub fn is_scan_id(s: &str) -> bool {
    let b = s.as_bytes();
    let date = b.len() >= 10 && b[..10].iter().enumerate().all(|(i, c)| if i == 4 || i == 7 { *c == b'-' } else { c.is_ascii_digit() });
    date && (b.len() == 10 || (b.len() == 15 && b[10] == b'T' && b[11..].iter().all(u8::is_ascii_digit)))
}

impl Scans {
    pub fn new(dir: PathBuf) -> Scans {
        Scans { dir, open: Default::default() }
    }

    /// Scan ids with both sorts written, newest first.
    pub fn list(&self) -> Vec<String> {
        let mut ids: Vec<String> = std::fs::read_dir(&self.dir)
            .into_iter()
            .flatten()
            .filter_map(|e| e.ok()?.file_name().into_string().ok())
            .filter(|n| is_scan_id(n) && self.dir.join(n).join(PATH_FILE).exists() && self.dir.join(n).join(BYSIZE_FILE).exists())
            .collect();
        ids.sort_by(|a, b| b.cmp(a));
        ids
    }

    pub fn get(&self, id: &str) -> io::Result<Arc<Scan>> {
        if !is_scan_id(id) {
            return Err(io::Error::new(io::ErrorKind::NotFound, format!("bad scan id {id:?}")));
        }
        if let Some(s) = self.open.lock().unwrap().get(id) {
            return Ok(s.clone());
        }
        let s = Arc::new(Scan::open(&self.dir.join(id))?);
        self.open.lock().unwrap().insert(id.into(), s.clone());
        Ok(s)
    }
}

/// `/api/series` for `path` over every scan, oldest first.
pub fn series(scans: &Scans, path: &str, paths: &[String], split_roots: bool) -> io::Result<Value> {
    let mut ids = scans.list();
    ids.reverse();
    let mut points = vec![];
    let mut traces: Vec<(String, Vec<Value>)> = vec![];
    let mut latest: HashMap<String, i64> = HashMap::new();
    for id in &ids {
        let scan = scans.get(id)?;
        if split_roots {
            let rs = roots(&scan)?;
            let (b, o) = rs.iter().fold((0, 0), |(b, o), r| (b + r.1, o + r.2));
            points.push(json!({"date": id, "b": b, "o": o}));
            latest.clear();
            for (p, b, o) in rs {
                latest.insert(p.clone(), b);
                match traces.iter_mut().find(|t| t.0 == p) {
                    Some(t) => t.1.push(json!({"date": id, "b": b, "o": o})),
                    None => traces.push((p, vec![json!({"date": id, "b": b, "o": o})])),
                }
            }
        } else if !paths.is_empty() {
            // Σ over the match roots; a root absent from a scan adds 0.
            let parts = paths.iter().map(|p| point(&scan, p)).collect::<io::Result<Vec<_>>>()?;
            if parts.iter().any(Option::is_some) {
                let (b, o) = parts.iter().flatten().fold((0, 0), |(b, o), x| (b + x.0, o + x.1));
                points.push(json!({"date": id, "b": b, "o": o}));
            }
        } else if let Some((b, o)) = point(&scan, path)? {
            points.push(json!({"date": id, "b": b, "o": o}));
        }
    }
    let mut body = json!({"path": path});
    if !paths.is_empty() {
        body["paths"] = json!(paths);
    }
    body["points"] = points.into();
    if split_roots {
        traces.sort_by(|a, b| {
            let first = |t: &(String, Vec<Value>)| t.1[0]["date"].as_str().unwrap_or_default().to_string();
            first(a).cmp(&first(b)).then_with(|| latest.get(&b.0).unwrap_or(&0).cmp(latest.get(&a.0).unwrap_or(&0))).then_with(|| a.0.cmp(&b.0))
        });
        body["roots"] = traces.into_iter().map(|(p, pts)| json!({"path": p, "points": pts})).collect::<Vec<_>>().into();
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_ids() {
        assert_eq!(["2026-10-02", "2026-10-02T1530", "2026-10-2", "2026-10-02T15", "../x"].map(is_scan_id), [true, true, false, false, false]);
    }

    #[test]
    fn rounding_and_paths() {
        assert_eq!([js_round(2.5), js_round(-2.5), js_round(0.49)], [3, -2, 0]);
        assert_eq!((parent_of("a/b/c"), parent_of("a"), depth_of(""), depth_of("a/b")), ("a/b", "", 0, 2));
        assert_eq!(under("a/b"), ("a/b/".to_string(), Some("a/b0".to_string())));
    }

    /// The tree as `(path, b, o, match)` rows, depth first.
    fn flat(n: &Value, p: &str, out: &mut Vec<(String, i64, i64, bool)>) {
        out.push((p.to_string(), n["b"].as_i64().unwrap(), n["o"].as_i64().unwrap(), n.get("m").is_some()));
        for c in n.get("c").and_then(Value::as_array).into_iter().flatten() {
            let name = c["n"].as_str().unwrap();
            flat(c, &if p.is_empty() { name.to_string() } else { format!("{p}/{name}") }, out);
        }
    }

    #[test]
    fn filter_view() {
        let ms = 20728 * 86_400_000;
        let mut r = crate::Reducer::new(20728);
        for (name, size) in [("a/node_modules/x", 4096), ("a/node_modules/.pnpm/y", 8192), ("a/src/node_modules_util.js", 512), ("b/node_modules/z", 2048), ("c/other", 1000)] {
            r.push("/", name, size, ms);
        }
        let dir = std::env::temp_dir().join(format!("dt-index-filter-view-{}", std::process::id()));
        r.finish().write_index(&dir).unwrap();
        let scan = Scan::open(&dir).unwrap();
        let view = |q: &str, path: &str| {
            let q = crate::query::parse(q, None).unwrap().unwrap();
            let o = ViewOpts { path, w: 1280.0, h: 768.0, min_area: 12.0, atten: 2.0, max_depth: None, root_label: "root", query: Some(&q) };
            subtree(&scan, &o).unwrap().unwrap()
        };
        let v = view("node_modules -.pnpm", "");
        assert_eq!(v["matches"], json!(["a/node_modules", "a/src/node_modules_util.js", "b/node_modules"]));
        assert_eq!(
            v["matched"],
            json!([{"path": "a/node_modules", "b": 4096, "o": 1}, {"path": "b/node_modules", "b": 2048, "o": 1}, {"path": "a/src/node_modules_util.js", "b": 512, "o": 1}])
        );
        assert_eq!(v["excluded"], json!(["a/node_modules/.pnpm"]));
        let mut rows = vec![];
        flat(&v["tree"], "", &mut rows);
        let row = |p: &str, b, o, m| (p.to_string(), b, o, m);
        assert_eq!(
            rows,
            [
                row("", 6656, 3, false),
                row("a", 4608, 2, false),
                row("a/node_modules", 4096, 1, true),
                row("a/node_modules/x", 4096, 1, false),
                row("a/src", 512, 1, false),
                row("a/src/node_modules_util.js", 512, 1, true),
                row("b", 2048, 1, false),
                row("b/node_modules", 2048, 1, true),
                row("b/node_modules/z", 2048, 1, false),
            ]
        );
        // Only negatives: the view root matches, less the exclusion.
        let v = view("-.pnpm", "a");
        assert_eq!((v["matches"].clone(), v["excluded"].clone(), v["tree"]["b"].clone()), (json!(["a"]), json!(["a/node_modules/.pnpm"]), json!(4608)));
        // The root itself matching, nothing to exclude: the plain view, marked.
        let v = view("node_modules", "b/node_modules");
        assert_eq!((v["matches"].clone(), v["matched"].clone(), v["tree"]["m"].clone(), v["tree"]["b"].clone()), (json!(["b/node_modules"]), json!([]), json!(1), json!(2048)));
        // No match: an empty tree.
        let v = view("zzzz", "");
        assert_eq!((v["matches"].clone(), v["tree"]["b"].clone(), v["tier"].clone()), (json!([]), json!(0), json!("none")));
        std::fs::remove_dir_all(&dir).ok();
    }
}
