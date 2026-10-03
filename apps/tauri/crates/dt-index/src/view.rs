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

#[derive(Clone, Copy)]
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
    /// A byte floor to read at instead of the canvas's (the diff's shared one).
    pub threshold: Option<f64>,
}

/// One view read: the kept paths and what renders them (`view.ts` `Read`).
struct Read {
    /// P's aggregate (a filter view: the matched total).
    root: Agg,
    root_name: String,
    /// Kept paths under P: `(depth, aggregate)`.
    aggs: HashMap<String, (i32, Agg)>,
    threshold: f64,
    /// `(other)`'s threshold: `thr · atten^(d − depth − 1)` for `(thr, depth)`.
    other: (f64, i32),
    tier: &'static str,
    truncated: bool,
    /// Sub-threshold children counted per kept parent (a filter view's `f`).
    folded: HashMap<String, i64>,
    marked: HashSet<String>,
    filter: Option<Filter>,
}

/// A filter view's matches, exactly.
struct Filter {
    matches: Vec<String>,
    /// `(path, b, o)`, bytes descending (capped at `HARD_CAP`).
    matched: Vec<(String, i64, i64)>,
    roots: PathSet,
    excl: PathSet,
    /// Each exclusion's own aggregate.
    excl_aggs: HashMap<String, Agg>,
    /// Every match root's net aggregate, by path.
    nets: Vec<(String, Agg)>,
}

impl Filter {
    /// Whether `p` is in a match root and not excluded.
    fn holds(&self, p: &str) -> bool {
        let d = depth_of(p);
        (self.roots.contains(p) || self.roots.above(p, d).is_some()) && !self.excl.contains(p) && self.excl.above(p, d).is_none()
    }

    /// Σ the excluded aggregates strictly under `p`.
    fn cut_under(&self, p: &str) -> Option<Agg> {
        let mut cut: Option<Agg> = None;
        for (e, a) in &self.excl_aggs {
            if p.is_empty() || e.strip_prefix(p).is_some_and(|r| r.starts_with('/')) {
                cut.get_or_insert_with(Agg::default).add(a);
            }
        }
        cut
    }

    /// Σ the net aggregates of the match roots strictly under `p` (what a
    /// root's ancestor holds in a filter view).
    fn roots_under(&self, p: &str) -> Option<Agg> {
        let (lo, hi) = under(p);
        let i = self.nets.partition_point(|x| x.0 < lo);
        let mut out: Option<Agg> = None;
        for (r, a) in &self.nets[i..] {
            if hi.as_ref().is_some_and(|h| r >= h) {
                break;
            }
            out.get_or_insert_with(|| Agg { dir: Some(true), ..Default::default() }).add(a);
        }
        out
    }
}

/// P's own aggregate in `scan` (the store root: its depth-1 rows summed).
fn root_agg(scan: &Scan, path: &str) -> io::Result<Option<Agg>> {
    let rows = scan.rect(&Rect::at(path))?;
    if rows.is_empty() {
        return Ok(None);
    }
    let mut root = Agg::default();
    for r in &rows {
        root.merge(r);
    }
    if path.is_empty() {
        root.nc = Some(rows.len() as i64);
        root.dir = Some(true);
    }
    Ok(Some(root))
}

/// `/api/subtree`'s body fields: `{tier, index, threshold, nodes, truncated, tree}`.
pub fn subtree(scan: &Scan, o: &ViewOpts) -> io::Result<Option<Map<String, Value>>> {
    let Some(root) = root_agg(scan, o.path)? else { return Ok(None) };
    let name = root_name(o);
    Ok(Some(match read(scan, o, root, &name)? {
        Some(v) => render(&v, o),
        None => empty_body(&name, o.query.is_some()),
    }))
}

fn root_name(o: &ViewOpts) -> String {
    if o.path.is_empty() { o.root_label.to_string() } else { o.path.rsplit('/').next().unwrap().to_string() }
}

/// The empty view: a zero root, or a filter with no matches.
fn empty_body(name: &str, query: bool) -> Map<String, Value> {
    let mut n = Map::new();
    n.insert("n".into(), name.into());
    n.insert("k".into(), "dir".into());
    n.insert("b".into(), 0.into());
    n.insert("o".into(), 0.into());
    let mut body = Map::new();
    body.extend([("tier".into(), "none".into()), ("index".into(), "none".into()), ("threshold".into(), 0.into()), ("nodes".into(), 0.into()), ("truncated".into(), false.into())]);
    if query {
        body.extend([("matches".into(), json!([])), ("matched".into(), json!([]))]);
    }
    body.insert("tree".into(), n.into());
    body
}

/// A view of P whose own aggregate is `root`; `None`: a zero root, or a
/// filter with no matches.
fn read(scan: &Scan, o: &ViewOpts, root: Agg, root_name: &str) -> io::Result<Option<Read>> {
    let path = o.path;
    let dp = depth_of(path);
    if root.b <= 0.0 {
        return Ok(None);
    }
    // A filter view, unless the root itself matches with nothing to exclude
    // (then the plain view is the answer, the root its one match).
    let root_hit = o.query.is_some_and(|q| q.matches(path));
    if let Some(q) = o.query.filter(|q| !root_hit || q.has_neg()) {
        return filtered(scan, o, q, &root, root_name);
    }
    let threshold = o.threshold.unwrap_or(root.b * o.min_area / (o.w * o.h));
    let thr_at = |d: i32| threshold * o.atten.powi((d - dp - 1).max(0));
    let (lo, hi) = under(path);
    let d_hi = o.max_depth.map_or(i32::MAX, |m| dp + m);
    let (rows, tier) = scan.subtree(&Rect { d: (dp + 1, d_hi), lo, hi }, threshold)?;
    let mut aggs: HashMap<String, (i32, Agg)> = HashMap::new();
    for r in rows {
        if (r.size as f64) < thr_at(r.depth) {
            continue;
        }
        aggs.entry(r.path.clone()).or_insert_with(|| (r.depth, Agg::default())).1.merge(&r);
    }
    aggs.retain(|_, (_, a)| a.b > 0.0);
    let truncated = aggs.len() > HARD_CAP;
    if truncated {
        let mut by_b: Vec<(String, f64)> = aggs.iter().map(|(p, (_, a))| (p.clone(), a.b)).collect();
        by_b.sort_by(|a, b| b.1.total_cmp(&a.1));
        for (p, _) in &by_b[HARD_CAP..] {
            aggs.remove(p);
        }
    }
    let filter = root_hit.then(|| {
        let mut roots = PathSet::default();
        roots.insert(path.into());
        Filter { matches: vec![path.into()], matched: vec![], roots, excl: PathSet::default(), excl_aggs: HashMap::new(), nets: vec![(path.into(), root.clone())] }
    });
    Ok(Some(Read {
        root,
        root_name: root_name.into(),
        aggs,
        threshold,
        other: (threshold, dp),
        tier,
        truncated,
        folded: HashMap::new(),
        marked: if root_hit { [path.to_string()].into() } else { HashSet::new() },
        filter,
    }))
}

/// A read's `/api/subtree` body.
fn render(v: &Read, o: &ViewOpts) -> Map<String, Value> {
    let path = o.path;
    // Nest in `(depth, path)` order so equal-byte siblings keep it (the
    // site's stable sort over its read order).
    let mut order: Vec<String> = v.aggs.keys().cloned().collect();
    order.sort_by(|a, b| v.aggs[a].0.cmp(&v.aggs[b].0).then_with(|| a.cmp(b)));
    let kept: HashSet<&str> = order.iter().map(String::as_str).collect();
    let kids = kids_index(&order, &kept, path);
    let thr_at = |d: i32| v.other.0 * o.atten.powi((d - v.other.1 - 1).max(0));
    let tree = build(path, &v.root, &v.root_name, &v.aggs, &kids, &thr_at, &v.folded, &v.marked);
    let mut body = Map::new();
    body.extend([
        ("tier".into(), v.tier.into()),
        ("index".into(), "footer".into()),
        ("threshold".into(), js_round(v.threshold).into()),
        ("nodes".into(), order.len().into()),
        ("truncated".into(), v.truncated.into()),
    ]);
    if let Some(f) = &v.filter {
        body.insert("matches".into(), json!(f.matches));
        body.insert("matched".into(), f.matched.iter().map(|(p, b, o)| json!({"path": p, "b": b, "o": o})).collect::<Vec<_>>().into());
        if !f.excl.set.is_empty() {
            let mut e: Vec<&String> = f.excl.set.iter().collect();
            e.sort();
            body.insert("excluded".into(), json!(e));
        }
    }
    body.insert("tree".into(), tree);
    body
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
fn filtered(scan: &Scan, o: &ViewOpts, q: &Query, root_all: &Agg, root_name: &str) -> io::Result<Option<Read>> {
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
    let matched_b = roots.set.iter().map(|r| if r == path { root_all.b } else { bytes[r] as f64 }).sum::<f64>() - excl.set.iter().map(|e| bytes[e] as f64).sum::<f64>();
    if roots.set.is_empty() || matched_b <= 0.0 {
        return Ok(None);
    }
    let thr = o.threshold.unwrap_or(matched_b * o.min_area / (o.w * o.h));
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
    let mut nets = vec![];
    let mut below: Vec<String> = vec![];
    for r in &roots.set {
        let (d, a) = &aggs1[r];
        let a = net(r, a);
        matched.add(&a);
        matched_list.push((r.clone(), js_round(a.b), js_round(a.o)));
        nets.push((r.clone(), a.clone()));
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
    let mut root_agg = matched;
    root_agg.dir = Some(true);
    matched_list.sort_by(|x, y| y.1.cmp(&x.1).then_with(|| x.0.cmp(&y.0)));
    matched_list.truncate(HARD_CAP);
    let mut matches: Vec<String> = matched_list.iter().map(|x| x.0.clone()).collect();
    matches.sort();
    nets.sort_by(|x, y| x.0.cmp(&y.0));
    let excl_aggs = excl.set.iter().map(|e| (e.clone(), aggs1[e].1.clone())).collect();
    Ok(Some(Read {
        root: root_agg,
        root_name: root_name.into(),
        aggs,
        threshold: thr,
        other: (thr, deepest),
        tier: "scan+path",
        truncated: roots.set.len() > HARD_CAP,
        folded,
        marked: roots.set.clone(),
        filter: Some(Filter { matches, matched: matched_list, roots, excl, excl_aggs, nets }),
    }))
}

/// `/api/diff`'s body fields (`view.ts` `buildDiff`): what changed under P
/// between scans `a` and `b`, both read at ONE byte floor (the larger side's
/// pixel threshold), so a path is named on both sides or folded on both. A
/// name one side kept and the other didn't is read on that other side by an
/// exact point lookup (it existed below the floor, or it was added /
/// removed); `(other)` is the parent less its named children on each side.
/// Read exactly, so there's no lookup budget (`lookups_capped` is false).
/// With `q=`, each side is its filter view (planned by its own matched
/// bytes, then re-read at the larger threshold); lookups see only matched,
/// un-excluded bytes.
pub fn diff(sa: &Scan, sb: &Scan, o: &ViewOpts, top: usize, summary: bool) -> io::Result<Option<Map<String, Value>>> {
    let path = o.path;
    let dp = depth_of(path);
    let (ra, rb) = (root_agg(sa, path)?, root_agg(sb, path)?);
    if ra.is_none() && rb.is_none() {
        return Ok(None);
    }
    let threshold = ra.iter().chain(&rb).map(|a| a.b).fold(0.0, f64::max) * o.min_area / (o.w * o.h);
    let name = root_name(o);
    let side = |s: &Scan, r: &Option<Agg>, thr: Option<f64>| -> io::Result<Option<Read>> {
        match r {
            Some(root) => read(s, &ViewOpts { threshold: thr, ..*o }, root.clone(), &name),
            None => Ok(None),
        }
    };
    let floor = o.query.is_none().then_some(threshold);
    let (mut va, mut vb) = std::thread::scope(|s| {
        let h = s.spawn(|| side(sb, &rb, floor));
        let a = side(sa, &ra, floor);
        (a, h.join().unwrap())
    });
    let (va0, vb0) = (va.as_ref().map_err(err)?, vb.as_ref().map_err(err)?);
    // A filtered diff plans each side by its own matched bytes; the shared
    // floor is the larger, and the other side is re-read at it.
    if let (Some(x), Some(y)) = (va0, vb0) {
        if o.query.is_some() && x.threshold != y.threshold {
            let shared = x.threshold.max(y.threshold);
            if x.threshold < shared {
                va = side(sa, &ra, Some(shared));
            } else {
                vb = side(sb, &rb, Some(shared));
            }
        }
    }
    let (va, vb) = (va?, vb?);
    let mut body = Map::new();
    let Some(head) = vb.as_ref().or(va.as_ref()) else {
        body.extend([("rows".into(), json!([])), ("total_a".into(), 0.into()), ("total_b".into(), 0.into()), ("objects_a".into(), 0.into()), ("objects_b".into(), 0.into()), ("threshold".into(), 0.into()), ("tier".into(), "none".into())]);
        if o.query.is_some() {
            body.insert("matched".into(), json!([]));
        }
        body.extend([("expansions".into(), 0.into()), ("truncated".into(), false.into()), ("lookups".into(), 0.into()), ("lookups_capped".into(), false.into())]);
        return Ok(Some(body));
    };
    let tot = |v: &Option<Read>, f: fn(&Agg) -> f64| v.as_ref().map_or(0, |v| js_round(f(&v.root)));
    let mut totals = Map::new();
    totals.extend([
        ("total_a".into(), tot(&va, |a| a.b).into()),
        ("total_b".into(), tot(&vb, |a| a.b).into()),
        ("objects_a".into(), tot(&va, |a| a.o).into()),
        ("objects_b".into(), tot(&vb, |a| a.o).into()),
        ("threshold".into(), js_round(if o.query.is_some() { head.threshold } else { threshold }).into()),
        ("tier".into(), head.tier.into()),
    ]);
    if o.query.is_some() {
        // The union of both sides' match roots, by path.
        let mut m: Vec<(String, i64, i64)> = vec![];
        let mut seen: HashMap<String, usize> = HashMap::new();
        for v in [&va, &vb].into_iter().flatten() {
            for x in v.filter.iter().flat_map(|f| &f.matched) {
                match seen.get(&x.0) {
                    Some(&i) => m[i] = x.clone(),
                    None => {
                        seen.insert(x.0.clone(), m.len());
                        m.push(x.clone());
                    }
                }
            }
        }
        m.sort_by(|x, y| x.0.cmp(&y.0));
        totals.insert("matched".into(), m.iter().map(|(p, b, o)| json!({"path": p, "b": b, "o": o})).collect::<Vec<_>>().into());
    }
    if summary {
        body.insert("rows".into(), json!([]));
        body.extend(totals);
        body.extend([("expansions".into(), 0.into()), ("truncated".into(), false.into()), ("lookups".into(), 0.into()), ("lookups_capped".into(), false.into())]);
        return Ok(Some(body));
    }
    let kids_of = |v: &Option<Read>| -> HashMap<String, Vec<String>> {
        let Some(v) = v else { return HashMap::new() };
        let mut order: Vec<&String> = v.aggs.keys().collect();
        order.sort();
        let mut kids: HashMap<String, Vec<String>> = HashMap::new();
        for p in order {
            let par = parent_of(p);
            let key = if v.aggs.contains_key(par) { par } else { path };
            kids.entry(key.into()).or_default().push(p.clone());
        }
        kids
    };
    let (kids_a, kids_b) = (kids_of(&va), kids_of(&vb));
    let rel = |p: &str| if path.is_empty() { p.to_string() } else { p[path.len() + 1..].to_string() };
    let rnd = |a: &Option<Agg>| a.as_ref().map_or((0, 0), |a| (js_round(a.b), js_round(a.o)));
    let status = |a: &Option<Agg>, b: &Option<Agg>| match (a, b) {
        (None, _) => "added",
        (_, None) => "removed",
        _ if rnd(a) != rnd(b) => "changed",
        _ => "unchanged",
    };
    struct Row {
        v: Value,
        x: bool,
        changed: bool,
        delta: i64,
    }
    let mut rows: Vec<Row> = vec![];
    let mut emit = |p: String, d: i32, a: &Option<Agg>, b: &Option<Agg>, x: bool, l: Option<u8>| {
        let k = match a.as_ref().or(b.as_ref()).and_then(|a| a.dir) {
            Some(false) => "file",
            _ => "dir",
        };
        let s = status(a, b);
        let ((ab, oa), (bb, ob)) = (rnd(a), rnd(b));
        let mut v = json!({"p": p, "d": d, "k": k, "s": s, "a": ab, "b": bb, "oa": oa, "ob": ob});
        if x {
            v["x"] = true.into();
        }
        if let Some(l) = l {
            v["l"] = l.into();
        }
        rows.push(Row { v, x, changed: s != "unchanged", delta: (bb - ab).abs() });
    };
    struct Item {
        p: String,
        d: i32,
        a: Option<Agg>,
        b: Option<Agg>,
        l: Option<u8>,
    }
    let mut level = vec![Item { p: path.into(), d: dp, a: va.as_ref().map(|v| v.root.clone()), b: vb.as_ref().map(|v| v.root.clone()), l: None }];
    let (mut expansions, mut lookups) = (0, 0);
    while !level.is_empty() {
        let plans: Vec<(bool, Vec<String>)> = level
            .iter()
            .map(|it| {
                let none = vec![];
                let ka = kids_a.get(&it.p).unwrap_or(&none);
                let kb = kids_b.get(&it.p).unwrap_or(&none);
                let same = it.a.is_some() && it.b.is_some() && rnd(&it.a) == rnd(&it.b);
                let expand = (it.a.is_some() || it.b.is_some()) && !same && (!ka.is_empty() || !kb.is_empty()) && o.max_depth.is_none_or(|m| it.d - dp < m);
                let mut names: Vec<String> = if expand { ka.iter().chain(kb).cloned().collect() } else { vec![] };
                names.sort();
                names.dedup();
                (expand, names)
            })
            .collect();
        // The names a side lacks, read on that side.
        let asks = |v: &Option<Read>| -> Vec<String> {
            let Some(v) = v else { return vec![] };
            plans.iter().flat_map(|(_, ns)| ns).filter(|n| !v.aggs.contains_key(*n)).cloned().collect()
        };
        let (asks_a, asks_b) = (asks(&va), asks(&vb));
        lookups += asks_a.len() + asks_b.len();
        let (got_a, got_b) = std::thread::scope(|s| {
            let h = s.spawn(|| lookup(sb, vb.as_ref(), &asks_b));
            let a = lookup(sa, va.as_ref(), &asks_a);
            (a, h.join().unwrap())
        });
        let (got_a, got_b) = (got_a?, got_b?);
        let mut next = vec![];
        for (it, (expand, names)) in level.iter().zip(plans) {
            if it.p != path {
                emit(rel(&it.p), it.d - dp, &it.a, &it.b, expand, it.l);
            }
            if !expand {
                continue;
            }
            expansions += 1;
            let (mut sa_, mut sb_) = (Agg::default(), Agg::default());
            for cp in names {
                let pick = |v: &Option<Read>, got: &HashMap<String, Agg>| v.as_ref().and_then(|v| v.aggs.get(&cp).map(|x| x.1.clone())).or_else(|| got.get(&cp).cloned());
                let (ca, cb) = (pick(&va, &got_a), pick(&vb, &got_b));
                let l = if ca.is_some() && got_a.contains_key(&cp) {
                    Some(1)
                } else if cb.is_some() && got_b.contains_key(&cp) {
                    Some(2)
                } else {
                    None
                };
                if let Some(c) = &ca {
                    sa_.b += c.b;
                    sa_.o += c.o;
                }
                if let Some(c) = &cb {
                    sb_.b += c.b;
                    sb_.o += c.o;
                }
                next.push(Item { p: cp, d: it.d + 1, a: ca, b: cb, l });
            }
            // A one-sided parent has no residual on its missing side.
            let rest = |x: &Option<Agg>, s: &Agg| {
                let a = Agg { b: (x.as_ref().map_or(0.0, |x| x.b) - s.b).max(0.0), o: (x.as_ref().map_or(0.0, |x| x.o) - s.o).max(0.0), ..Default::default() };
                (a.b > 0.0).then_some(a)
            };
            let (oa, ob) = (rest(&it.a, &sa_), rest(&it.b, &sb_));
            if oa.is_some() || ob.is_some() {
                let key = if it.p == path { "(other)".to_string() } else { format!("{}/(other)", rel(&it.p)) };
                emit(key, it.d - dp + 1, &oa, &ob, false, None);
            }
        }
        level = next;
    }
    // Every expanded ancestor (the skeleton), then the changed frontier
    // rows by |Δ|; unchanged frontier rows the renderer infers as filler.
    let mut frontier: Vec<&Row> = rows.iter().filter(|r| !r.x && r.changed).collect();
    frontier.sort_by(|x, y| y.delta.cmp(&x.delta));
    let truncated = frontier.len() > top;
    let out: Vec<Value> = rows.iter().filter(|r| r.x).chain(frontier.into_iter().take(top)).map(|r| r.v.clone()).collect();
    body.insert("rows".into(), out.into());
    body.extend(totals);
    body.extend([("expansions".into(), expansions.into()), ("truncated".into(), truncated.into()), ("lookups".into(), lookups.into()), ("lookups_capped".into(), false.into())]);
    Ok(Some(body))
}

/// Exact point reads of `asks` (paths) on one side of a diff: each path's
/// own row(s) from the `path` sort; under a filter, only matched,
/// un-excluded bytes (a root's ancestor: Σ its roots' net bytes).
fn lookup(scan: &Scan, v: Option<&Read>, asks: &[String]) -> io::Result<HashMap<String, Agg>> {
    let mut out = HashMap::new();
    let Some(v) = v else { return Ok(out) };
    let mut todo: Vec<&String> = vec![];
    for p in asks {
        match &v.filter {
            Some(f) if !f.holds(p) => {
                if let Some(a) = f.excl.above(p, depth_of(p)).is_none().then(|| f.roots_under(p)).flatten() {
                    out.insert(p.clone(), a);
                }
            }
            _ => todo.push(p),
        }
    }
    if todo.is_empty() {
        return Ok(out);
    }
    let rects: Vec<Rect> = todo.iter().map(|p| Rect::at(p)).collect();
    let want: HashSet<&str> = todo.iter().map(|p| p.as_str()).collect();
    let groups: Vec<usize> = (0..scan.path.groups.len()).filter(|&g| rects.iter().any(|r| scan.path.groups[g].meets(r))).collect();
    let got = scan.path.par_groups(groups, |g| Ok(scan.path.read(vec![g])?.into_iter().filter(|r| want.contains(r.path.as_str())).collect::<Vec<_>>()))?;
    let mut aggs: HashMap<String, Agg> = HashMap::new();
    for r in got.into_iter().flatten() {
        aggs.entry(r.path.clone()).or_default().merge(&r);
    }
    for (p, a) in aggs {
        let a = match &v.filter {
            Some(f) => a.minus(f.cut_under(&p).as_ref(), 0),
            None => a,
        };
        if a.b > 0.0 {
            out.insert(p, a);
        }
    }
    Ok(out)
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
            let o = ViewOpts { path, w: 1280.0, h: 768.0, min_area: 12.0, atten: 2.0, max_depth: None, root_label: "root", query: Some(&q), threshold: None };
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

    fn store(tag: &str, files: &[(&str, i64)]) -> (PathBuf, Scan) {
        let ms = 20728 * 86_400_000;
        let mut r = crate::Reducer::new(20728);
        for (name, size) in files {
            r.push("/", name, *size, ms);
        }
        let dir = std::env::temp_dir().join(format!("dt-index-diff-{tag}-{}", std::process::id()));
        r.finish().write_index(&dir).unwrap();
        let scan = Scan::open(&dir).unwrap();
        (dir, scan)
    }

    /// `(p, s, a, b, oa, ob, x)` per row.
    fn diff_rows(d: &Map<String, Value>) -> Vec<(String, String, i64, i64, i64, i64, bool)> {
        d["rows"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| (r["p"].as_str().unwrap().into(), r["s"].as_str().unwrap().into(), r["a"].as_i64().unwrap(), r["b"].as_i64().unwrap(), r["oa"].as_i64().unwrap(), r["ob"].as_i64().unwrap(), r.get("x").is_some()))
            .collect()
    }

    #[test]
    fn diff_view() {
        let (da, sa) = store("a", &[("a/x", 4096), ("a/y", 4096), ("b/z", 8192), ("c/w", 1000)]);
        let (db, sb) = store("b", &[("a/x", 4096), ("a/y", 8192), ("c/w", 1000), ("d/n", 2048)]);
        let run = |q: Option<&Query>, summary: bool| {
            let o = ViewOpts { path: "", w: 1280.0, h: 768.0, min_area: 12.0, atten: 2.0, max_depth: None, root_label: "root", query: q, threshold: None };
            diff(&sa, &sb, &o, 500, summary).unwrap().unwrap()
        };
        let row = |p: &str, s: &str, a, b, oa, ob, x| (p.to_string(), s.to_string(), a, b, oa, ob, x);
        let d = run(None, false);
        // The skeleton (expanded rows, walk order), then the changed frontier
        // by |Δ|; `c` (unchanged) and `a/x` (unchanged) are left to the renderer.
        assert_eq!(
            diff_rows(&d),
            [
                row("a", "changed", 8192, 12288, 2, 2, true),
                row("b", "removed", 8192, 0, 1, 0, true),
                row("d", "added", 0, 2048, 0, 1, true),
                row("b/z", "removed", 8192, 0, 1, 0, false),
                row("a/y", "changed", 4096, 8192, 1, 1, false),
                row("d/n", "added", 0, 2048, 0, 1, false),
            ]
        );
        assert_eq!((d["total_a"].clone(), d["total_b"].clone(), d["objects_a"].clone(), d["objects_b"].clone(), d["expansions"].clone()), (json!(17384), json!(15336), json!(4), json!(4), json!(4)));
        let s = run(None, true);
        assert_eq!((s["rows"].clone(), s["total_a"].clone(), s["total_b"].clone()), (json!([]), json!(17384), json!(15336)));
        // Filtered: each side's matched bytes only.
        let q = crate::query::parse("a/y|d/n", None).unwrap().unwrap();
        let d = run(Some(&q), false);
        assert_eq!(
            diff_rows(&d),
            [
                row("a", "changed", 4096, 8192, 1, 1, true),
                row("d", "added", 0, 2048, 0, 1, true),
                row("a/y", "changed", 4096, 8192, 1, 1, false),
                row("d/n", "added", 0, 2048, 0, 1, false),
            ]
        );
        assert_eq!(d["matched"], json!([{"path": "a/y", "b": 8192, "o": 1}, {"path": "d/n", "b": 2048, "o": 1}]));
        assert_eq!((d["total_a"].clone(), d["total_b"].clone()), (json!(4096), json!(10240)));
        for dir in [da, db] {
            std::fs::remove_dir_all(dir).ok();
        }
    }
}
