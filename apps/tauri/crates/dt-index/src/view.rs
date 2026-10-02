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

use std::collections::HashMap;
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::cast::AsArray;
use arrow_array::types::{Float64Type, Int32Type, Int64Type};
use arrow_array::{Array, RecordBatch};
use parquet::arrow::arrow_reader::{ArrowReaderMetadata, ParquetRecordBatchReaderBuilder};
use parquet::arrow::ProjectionMask;
use parquet::file::statistics::Statistics;
use serde_json::{json, Map, Value};

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
        if groups.is_empty() {
            return Ok(vec![]);
        }
        let schema = self.meta.parquet_schema();
        let leaves: Vec<usize> = (0..schema.num_columns())
            .filter(|&i| {
                let c = schema.column(i);
                COLS.contains(&c.name()) || c.name().starts_with("age_b")
            })
            .collect();
        let reader = ParquetRecordBatchReaderBuilder::new_with_metadata(File::open(&self.file)?, self.meta.clone())
            .with_row_groups(groups)
            .with_projection(ProjectionMask::leaves(schema, leaves))
            .with_batch_size(8192)
            .build()
            .map_err(err)?;
        let mut out = vec![];
        for b in reader {
            decode(&b.map_err(err)?, &mut out)?;
        }
        Ok(out)
    }
}

fn decode(b: &RecordBatch, out: &mut Vec<VRow>) -> io::Result<()> {
    let col = |n: &str| b.column_by_name(n).ok_or_else(|| err(format!("no `{n}` column")));
    let path = col("path")?.as_string::<i32>();
    let depth = col("depth")?.as_primitive::<Int32Type>();
    let kind = col("kind")?.as_string::<i32>();
    let size = col("size")?.as_primitive::<Int64Type>();
    let n_files = col("n_files")?.as_primitive::<Int64Type>();
    let n_children = col("n_children")?.as_primitive::<Int64Type>();
    let mean = col("mtime_mean")?.as_primitive::<Float64Type>();
    let ages: Vec<_> = (0..N_AGE).map(|i| b.column_by_name(&format!("age_b{i}")).map(|c| c.as_primitive::<Int64Type>().clone())).collect();
    for i in 0..b.num_rows() {
        let mut a = [0; N_AGE];
        for (j, c) in ages.iter().enumerate() {
            if let Some(c) = c {
                a[j] = c.value(i);
            }
        }
        out.push(VRow {
            path: path.value(i).to_string(),
            depth: depth.value(i),
            dir: kind.value(i) == "dir",
            size: size.value(i),
            n_files: n_files.value(i),
            n_children: n_children.value(i),
            mtime_mean: (!mean.is_null(i)).then(|| mean.value(i)),
            ages: a,
        });
    }
    Ok(())
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
    x.depth >= r.d.0 && x.depth <= r.d.1 && x.path.as_str() >= r.lo.as_str() && r.hi.as_ref().is_none_or(|h| x.path.as_str() < h.as_str())
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
        body.extend([("tier".into(), "none".into()), ("index".into(), "none".into()), ("threshold".into(), 0.into()), ("nodes".into(), 0.into()), ("truncated".into(), false.into()), ("tree".into(), n.into())]);
        return Ok(Some(body));
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
    let kept: std::collections::HashSet<&str> = order.iter().map(String::as_str).collect();
    let mut kids: HashMap<&str, Vec<&str>> = HashMap::new();
    for p in &order {
        let par = parent_of(p);
        let key = if kept.contains(par) { par } else { path };
        kids.entry(key).or_default().push(p);
    }
    fn build(p: &str, a: &Agg, name: &str, aggs: &HashMap<String, (i32, Agg)>, kids: &HashMap<&str, Vec<&str>>, thr_at: &dyn Fn(i32) -> f64) -> Value {
        let mut node = a.node(name);
        let Some(cs) = kids.get(p) else { return node.into() };
        let mut children: Vec<(f64, Value)> = cs
            .iter()
            .map(|c| {
                let (_, ca) = &aggs[*c];
                (js_round(ca.b) as f64, build(c, ca, c.rsplit('/').next().unwrap(), aggs, kids, thr_at))
            })
            .collect();
        children.sort_by(|x, y| y.0.total_cmp(&x.0));
        let kid_aggs: Vec<&Agg> = cs.iter().map(|c| &aggs[*c].1).collect();
        let rest = a.subtract(&kid_aggs);
        let mut c: Vec<Value> = children.into_iter().map(|(_, v)| v).collect();
        if rest.b > thr_at(aggs[cs[0]].0) {
            let mut other = rest.node("(other)");
            other.insert("f".into(), (a.nc.unwrap_or(0) - cs.len() as i64).max(0).into());
            c.push(other.into());
        }
        node.insert("c".into(), c.into());
        node.into()
    }
    let tree = build(path, &root, &root_name, &aggs, &kids, &thr_at);
    body.extend([
        ("tier".into(), tier.into()),
        ("index".into(), "footer".into()),
        ("threshold".into(), js_round(threshold).into()),
        ("nodes".into(), order.len().into()),
        ("truncated".into(), truncated.into()),
        ("tree".into(), tree),
    ]);
    Ok(Some(body))
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
pub fn series(scans: &Scans, path: &str, split_roots: bool) -> io::Result<Value> {
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
        } else if let Some((b, o)) = point(&scan, path)? {
            points.push(json!({"date": id, "b": b, "o": o}));
        }
    }
    let mut body = json!({"path": path, "points": points});
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
}
