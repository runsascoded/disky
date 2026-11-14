//! A synthetic fleet's version history, in the folded path versions' shape (`interval_store.PVL_SCHEMA`), for
//! `oracle/synth_store.py` to cut into a real generation with the repo's own cut (`cut_gen` for the base,
//! `write_served` for each run).
//!
//! Shape: buckets of checkpoints (runs → steps → zarr-v3 tensor trees: `zarr.json` beside deep `c/i/j/k` chunk paths,
//! so many single-object dirs), tokenized datasets (flat dirs of `shard_NNNNN.npy`), raw ingest (one new day dir per
//! scan), users' dirs (notebooks, `node_modules` trees, mixed-case names) and logs (event files rewritten while their run
//! is active), with owners per run / user, a nearline lifecycle rule (a new version per object at its transition),
//! an archive bucket, step retention ("keep the last 3"), a sweep scan and scratch churn.
//!
//! Output (`-o STAGE`): `base/dNNN.parquet` (versions opened by the base's last scan, per depth; `vt` OPEN when still
//! open then), `runs/<scan>.parquet` (each run scan's opened versions and close records), `scans.json`.

use std::collections::BTreeMap;
use std::fs::File;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::{ArrayRef, Float64Array, Int32Array, Int64Array, RecordBatch, StringArray, UInt8Array};
use arrow_schema::{DataType, Field, Schema};
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;
use serde_json::json;

use crate::store::OPEN;
use crate::util::Result;

/// 2026-10-01 00:00 UTC.
const T0: i64 = 1_790_812_800;
const OWNERS: [&str; 8] = ["u00", "u01", "u02", "u03", "u04", "u05", "u06", "u07"];
const BUCKETS: [&str; 6] = ["bkt-us-central1", "bkt-us-central2", "bkt-us-east1", "bkt-us-east5", "bkt-eu-west4", "bkt-archive"];

fn schema() -> Arc<Schema> {
    let i64f = |n: &str| Field::new(n, DataType::Int64, false);
    Arc::new(Schema::new(vec![
        Field::new("depth", DataType::UInt8, false),
        Field::new("path", DataType::Utf8, false),
        i64f("vf"),
        i64f("vt"),
        Field::new("kind", DataType::Utf8, false),
        i64f("size"),
        i64f("n_files"),
        i64f("n_children"),
        i64f("n_desc"),
        i64f("mtime"),
        Field::new("wts", DataType::Float64, false),
        i64f("wb"),
        i64f("c2"),
        i64f("c3"),
        i64f("c4"),
        Field::new("us", DataType::Utf8, false),
        Field::new("last_read", DataType::Int32, false),
    ]))
}

#[derive(Default)]
struct Rows {
    depth: Vec<u8>,
    path: Vec<String>,
    vf: Vec<i64>,
    vt: Vec<i64>,
    kind: Vec<&'static str>,
    size: Vec<i64>,
    n_files: Vec<i64>,
    n_children: Vec<i64>,
    mtime: Vec<i64>,
    c: [Vec<i64>; 3],
    us: Vec<String>,
}

struct Out {
    w: ArrowWriter<File>,
    rows: Rows,
    n: u64,
}

impl Out {
    fn new(path: &Path) -> Result<Out> {
        let props = WriterProperties::builder().set_compression(Compression::LZ4_RAW).set_max_row_group_row_count(Some(1 << 17)).build();
        Ok(Out { w: ArrowWriter::try_new(File::create(path)?, schema(), Some(props))?, rows: Rows::default(), n: 0 })
    }

    fn flush(&mut self) -> Result<()> {
        let r = std::mem::take(&mut self.rows);
        if r.depth.is_empty() {
            return Ok(());
        }
        let n = r.depth.len();
        let wts: Vec<f64> = r.mtime.iter().zip(&r.size).map(|(&m, &s)| m as f64 * s as f64).collect();
        let [c2, c3, c4] = r.c;
        let cols: Vec<ArrayRef> = vec![
            Arc::new(UInt8Array::from(r.depth)),
            Arc::new(StringArray::from(r.path)),
            Arc::new(Int64Array::from(r.vf)),
            Arc::new(Int64Array::from(r.vt)),
            Arc::new(StringArray::from(r.kind)),
            Arc::new(Int64Array::from(r.size.clone())),
            Arc::new(Int64Array::from(r.n_files)),
            Arc::new(Int64Array::from(r.n_children)),
            Arc::new(Int64Array::from(vec![-1i64; n])),
            Arc::new(Int64Array::from(r.mtime)),
            Arc::new(Float64Array::from(wts)),
            Arc::new(Int64Array::from(r.size)),
            Arc::new(Int64Array::from(c2)),
            Arc::new(Int64Array::from(c3)),
            Arc::new(Int64Array::from(c4)),
            Arc::new(StringArray::from(r.us)),
            Arc::new(Int32Array::from(vec![-1i32; n])),
        ];
        self.w.write(&RecordBatch::try_new(schema(), cols)?)?;
        Ok(())
    }
}

/// One version's values (a file's or a directory's at a run of scans).
#[derive(Clone, PartialEq)]
struct Val {
    size: i64,
    n_files: i64,
    n_children: i64,
    mtime: i64,
    c: [i64; 3],
    us: String,
}

#[derive(Clone, Copy)]
struct FileVer {
    vf: usize,
    vt: usize,
    size: i64,
    cls: u8,
    owner: u8,
    mtime: i64,
}

/// A subtree's per-scan totals as difference arrays (`S + 1` long), so a file version adds in O(1).
#[derive(Clone)]
struct Series {
    /// size, n_files, c2, c3, c4, n_children.
    add: [Vec<i64>; 6],
    /// Newest version stamp opened by each scan (a prefix max gives a dir's mtime).
    mt: Vec<i64>,
    /// Owner index + 1 (0 = none) → (bytes, files) difference arrays.
    own: BTreeMap<u8, (Vec<i64>, Vec<i64>)>,
}

impl Series {
    fn new(s: usize) -> Series {
        Series { add: std::array::from_fn(|_| vec![0; s + 1]), mt: vec![i64::MIN; s], own: BTreeMap::new() }
    }

    fn file(&mut self, v: &FileVer) {
        let (a, z) = (v.vf, v.vt);
        self.add[0][a] += v.size;
        self.add[0][z] -= v.size;
        self.add[1][a] += 1;
        self.add[1][z] -= 1;
        if v.cls >= 2 {
            self.add[v.cls as usize][a] += v.size;
            self.add[v.cls as usize][z] -= v.size;
        }
        self.mt[a] = self.mt[a].max(v.mtime);
        let s = self.mt.len();
        let e = self.own.entry(v.owner).or_insert_with(|| (vec![0; s + 1], vec![0; s + 1]));
        e.0[a] += v.size;
        e.0[z] -= v.size;
        e.1[a] += 1;
        e.1[z] -= 1;
    }

    fn absorb(&mut self, o: &Series, live: &[bool]) {
        for k in 0..5 {
            for (x, y) in self.add[k].iter_mut().zip(&o.add[k]) {
                *x += y;
            }
        }
        for s in 0..live.len() {
            let was = s > 0 && live[s - 1];
            if live[s] != was {
                self.add[5][s] += if live[s] { 1 } else { -1 };
            }
        }
        if let Some(true) = live.last() {
            self.add[5][live.len()] -= 1;
        }
        for (x, y) in self.mt.iter_mut().zip(&o.mt) {
            *x = (*x).max(*y);
        }
        let s = self.mt.len();
        for (k, (b, f)) in &o.own {
            let e = self.own.entry(*k).or_insert_with(|| (vec![0; s + 1], vec![0; s + 1]));
            for i in 0..=s {
                e.0[i] += b[i];
                e.1[i] += f[i];
            }
        }
    }

    /// Per-scan values, and whether the dir is live (holds a live file) at each scan.
    fn values(&self) -> (Vec<Val>, Vec<bool>) {
        let s = self.mt.len();
        let mut cur = [0i64; 6];
        let mut mt = i64::MIN;
        let mut ocur: Vec<(u8, i64, i64)> = self.own.keys().map(|&k| (k, 0, 0)).collect();
        let mut vals = Vec::with_capacity(s);
        let mut live = Vec::with_capacity(s);
        for i in 0..s {
            for k in 0..6 {
                cur[k] += self.add[k][i];
            }
            mt = mt.max(self.mt[i]);
            for (j, (_, (b, f))) in self.own.iter().enumerate() {
                ocur[j].1 += b[i];
                ocur[j].2 += f[i];
            }
            // `us_sql_from_raw`: '' with no named owner; the name when the path is one (named) slice; else JSON.
            let named: Vec<&(u8, i64, i64)> = ocur.iter().filter(|o| o.0 != 0 && o.2 > 0).collect();
            let unnamed = ocur.iter().any(|o| o.0 == 0 && o.2 > 0);
            let us = if named.is_empty() {
                String::new()
            } else if named.len() == 1 && !unnamed {
                OWNERS[named[0].0 as usize - 1].to_string()
            } else {
                let parts: Vec<String> = named.iter().map(|o| format!("[\"{}\",{}]", OWNERS[o.0 as usize - 1], o.1)).collect();
                format!("[{}]", parts.join(","))
            };
            live.push(cur[1] > 0);
            vals.push(Val { size: cur[0], n_files: cur[1], n_children: cur[5], mtime: mt.max(0), c: [cur[2], cur[3], cur[4]], us });
        }
        (vals, live)
    }
}

pub struct Synth {
    s: usize,
    /// The base's last scan (ordinal); later scans are runs.
    k: usize,
    scale: f64,
    rng: u64,
    base: BTreeMap<u8, Out>,
    runs: Vec<Option<Out>>,
    stage: PathBuf,
    pub versions: u64,
    pub file_versions: u64,
    /// A counter in generated names, so no two runs, datasets or log dirs share a path.
    uid: u64,
}

fn ts(i: usize) -> i64 {
    T0 + i as i64 * 86_400
}

/// `YYYY-MM-DD` of an epoch day.
fn date(t: i64) -> String {
    let z = t.div_euclid(86_400) + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    format!("{:04}-{:02}-{:02}", if m <= 2 { y + 1 } else { y }, m, d)
}

impl Synth {
    fn rand(&mut self) -> u64 {
        self.rng = self.rng.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.rng;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.rand() % n.max(1)
    }
    fn p(&mut self, p: f64) -> bool {
        (self.rand() >> 11) as f64 / (1u64 << 53) as f64 <= p
    }
    /// A random-looking suffix that is unique in this fleet.
    fn uniq(&mut self) -> String {
        self.uid += 1;
        format!("{}{:05x}", self.hex(3), self.uid)
    }
    fn hex(&mut self, n: usize) -> String {
        let v = self.rand();
        format!("{v:016x}")[..n].to_string()
    }
    /// A log-uniform size in `[lo, hi)`.
    fn size(&mut self, lo: f64, hi: f64) -> i64 {
        let u = (self.rand() >> 11) as f64 / (1u64 << 53) as f64;
        (lo * (hi / lo).powf(u)) as i64
    }
    fn n(&mut self, base: f64) -> usize {
        ((base * self.scale) as usize).max(1)
    }

    fn row(&mut self, depth: u8, path: &str, vf: usize, vt: usize, kind: &'static str, v: &Val) -> Result<()> {
        self.versions += 1;
        if kind == "file" {
            self.file_versions += 1;
        }
        let push = |o: &mut Out, vtt: i64| -> Result<()> {
            let r = &mut o.rows;
            r.depth.push(depth);
            r.path.push(path.to_string());
            r.vf.push(ts(vf));
            r.vt.push(vtt);
            r.kind.push(kind);
            r.size.push(v.size);
            r.n_files.push(v.n_files);
            r.n_children.push(v.n_children);
            r.mtime.push(v.mtime);
            for i in 0..3 {
                r.c[i].push(v.c[i]);
            }
            r.us.push(v.us.clone());
            o.n += 1;
            if r.depth.len() >= 1 << 16 {
                o.flush()?;
            }
            Ok(())
        };
        let open_at = |end: usize, upto: usize| if end > upto { OPEN } else { ts(end) };
        if vf <= self.k {
            let stage = self.stage.clone();
            let o = match self.base.entry(depth) {
                std::collections::btree_map::Entry::Occupied(e) => e.into_mut(),
                std::collections::btree_map::Entry::Vacant(e) => e.insert(Out::new(&stage.join(format!("base/d{depth:03}.parquet")))?),
            };
            push(o, open_at(vt, self.k))?;
        } else {
            push(self.runs[vf].as_mut().unwrap(), OPEN)?;
        }
        if vt > self.k && vt < self.s {
            push(self.runs[vt].as_mut().unwrap(), ts(vt))?;
        }
        Ok(())
    }

    fn file(&mut self, parent: &str, name: &str, depth: u8, vers: &[FileVer], acc: &mut Series) -> Result<()> {
        if vers.is_empty() {
            return Ok(());
        }
        let path = format!("{parent}/{name}");
        for v in vers {
            let mut c = [0i64; 3];
            if v.cls >= 2 {
                c[v.cls as usize - 2] = v.size;
            }
            let us = if v.owner == 0 { String::new() } else { OWNERS[v.owner as usize - 1].to_string() };
            let val = Val { size: v.size, n_files: 1, n_children: 0, mtime: v.mtime, c, us };
            self.row(depth, &path, v.vf, v.vt, "file", &val)?;
            acc.file(v);
        }
        let (a, z) = (vers[0].vf, vers[vers.len() - 1].vt);
        acc.add[5][a] += 1;
        acc.add[5][z] -= 1;
        Ok(())
    }

    /// A directory: its children from `f`, then its own versions (runs of equal totals while it holds a live file).
    fn dir(&mut self, parent: &str, name: &str, depth: u8, acc: &mut Series, f: impl FnOnce(&mut Self, &str, u8, &mut Series) -> Result<()>) -> Result<()> {
        let path = if parent.is_empty() { name.to_string() } else { format!("{parent}/{name}") };
        let mut me = Series::new(self.s);
        f(self, &path, depth + 1, &mut me)?;
        let (vals, live) = me.values();
        let mut i = 0;
        while i < self.s {
            if !live[i] {
                i += 1;
                continue;
            }
            let mut j = i + 1;
            while j < self.s && live[j] && vals[j] == vals[i] {
                j += 1;
            }
            let v = vals[i].clone();
            self.row(depth, &path, i, j, "dir", &v)?;
            i = j;
        }
        acc.absorb(&me, &live);
        Ok(())
    }

    /// Versions of an object alive over `[born, died)`: rewritten with probability `rw` per scan, moved to `to_cls` at
    /// scan `lc` (a lifecycle rule) when given.
    #[allow(clippy::too_many_arguments)]
    fn life(&mut self, born: usize, died: usize, size: i64, cls: u8, owner: u8, rw: f64, lc: Option<(usize, u8)>) -> Vec<FileVer> {
        let mut out = Vec::new();
        if born >= died || born >= self.s {
            return out;
        }
        let mut cur = FileVer { vf: born, vt: died, size, cls, owner, mtime: ts(born) - 3600 - self.below(80_000) as i64 };
        for s in born + 1..died.min(self.s) {
            let rewrite = rw > 0.0 && self.p(rw);
            let moved = matches!(lc, Some((at, to)) if at == s && cur.cls != to);
            if rewrite || moved {
                let mut next = cur;
                cur.vt = s;
                out.push(cur);
                next.vf = s;
                if rewrite {
                    next.size = (next.size as f64 * (1.0 + (self.below(40) as f64) / 100.0)) as i64 + 1;
                    next.mtime = ts(s) - 3600 - self.below(80_000) as i64;
                }
                if let Some((at, to)) = lc {
                    if at == s {
                        next.cls = to;
                    }
                }
                cur = next;
            }
        }
        cur.vt = died.min(self.s);
        out.push(cur);
        out
    }

    fn born(&mut self, p0: f64) -> usize {
        if self.p(p0) { 0 } else { 1 + self.below(self.s as u64 - 1) as usize }
    }

    // ── The fleet ──

    fn tensor(&mut self, p: &str, d: u8, acc: &mut Series, born: usize, died: usize, cls: u8, owner: u8, lc: Option<(usize, u8)>) -> Result<()> {
        let zs = 1200 + self.below(800) as i64;
        let zj = self.life(born, died, zs, cls, owner, 0.0, lc);
        self.file(p, "zarr.json", d, &zj, acc)?;
        let (gi, gj) = (1 + self.below(3) as usize, 1 + self.below(3) as usize);
        let gk = 1 + self.below(2) as usize;
        self.dir(p, "c", d, acc, |me, p, d, acc| {
            for i in 0..gi {
                me.dir(p, &i.to_string(), d, acc, |me, p, d, acc| {
                    for j in 0..gj {
                        me.dir(p, &j.to_string(), d, acc, |me, p, d, acc| {
                            for k in 0..gk {
                                let sz = me.size(1e6, 6.4e7);
                                let v = me.life(born, died, sz, cls, owner, 0.0, lc);
                                me.file(p, &k.to_string(), d, &v, acc)?;
                            }
                            Ok(())
                        })?;
                    }
                    Ok(())
                })?;
            }
            Ok(())
        })
    }

    fn checkpoints(&mut self, p: &str, d: u8, acc: &mut Series, bucket: usize) -> Result<()> {
        let runs = self.n(6.0);
        for r in 0..runs {
            let owner = if self.p(0.1) { 0 } else { 1 + self.below(8) as u8 };
            let model = ["llama", "olmo", "tootsie", "marin", "gemma"][self.below(5) as usize];
            let name = format!("run-{model}-{}m-{}", [150, 300, 600, 1400, 8000][r % 5], self.uniq());
            let start = self.born(0.6);
            let steps = 4 + self.below(14) as usize;
            let every = 1 + self.below(3) as usize;
            let keep3 = self.p(0.3);
            // The sweep: scan 20 deletes a few runs entirely.
            let swept = self.p(0.08);
            let end = if swept { 20 } else { self.s };
            let cls = if bucket == 5 { 4 } else { 1 };
            let lc = if bucket == 2 && start == 0 { Some((15, 2)) } else { None };
            let layers = 2 + self.below(5) as usize;
            self.dir(p, &name, d, acc, |me, p, d, acc| {
                for st in 0..steps {
                    let born = start + st * every;
                    let died = if keep3 { (start + (st + 3) * every).min(end) } else { end };
                    if born >= me.s || born >= died {
                        continue;
                    }
                    me.dir(p, &format!("step-{:06}", (st + 1) * 1000), d, acc, |me, p, d, acc| {
                        let v = me.life(born, died, 900, cls, owner, 0.0, lc);
                        me.file(p, "_CHECKPOINT_METADATA", d, &v, acc)?;
                        let v = me.life(born, died, 40_000, cls, owner, 0.0, lc);
                        me.file(p, &format!("ckpt-{}.index", (st + 1) * 1000), d, &v, acc)?;
                        me.dir(p, "params", d, acc, |me, p, d, acc| {
                            for l in 0..layers {
                                me.dir(p, &format!("layer_{l}"), d, acc, |me, p, d, acc| {
                                    for t in ["attn", "mlp", "norm"] {
                                        me.dir(p, t, d, acc, |me, p, d, acc| me.tensor(p, d, acc, born, died, cls, owner, lc))?;
                                    }
                                    Ok(())
                                })?;
                            }
                            Ok(())
                        })?;
                        me.dir(p, "train_state", d, acc, |me, p, d, acc| {
                            me.dir(p, "opt_state", d, acc, |me, p, d, acc| {
                                for t in ["mu", "nu"] {
                                    me.dir(p, t, d, acc, |me, p, d, acc| me.tensor(p, d, acc, born, died, cls, owner, lc))?;
                                }
                                Ok(())
                            })
                        })
                    })?;
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    fn tokenized(&mut self, p: &str, d: u8, acc: &mut Series, bucket: usize) -> Result<()> {
        let sets = self.n(3.0);
        for _ in 0..sets {
            let words = ["fineweb", "dclm", "starcoder", "arxiv", "wiki", "xylophone", "pubmed", "stackexchange"];
            let name = format!("{}-{}", words[self.below(words.len() as u64) as usize], self.uniq());
            let born = self.born(0.8);
            let owner = if self.p(0.5) { 0 } else { 1 + self.below(8) as u8 };
            let shards = 50 + self.below(600) as usize;
            let cls = if bucket == 5 { 4 } else { 1 };
            self.dir(p, &name, d, acc, |me, p, d, acc| {
                let v = me.life(born, me.s, 3000, cls, owner, 0.0, None);
                me.file(p, ".stats.json", d, &v, acc)?;
                let v = me.life(born, me.s, 500, cls, owner, 0.0, None);
                me.file(p, "ledger.json", d, &v, acc)?;
                for split in ["train", "validation"] {
                    let n = if split == "train" { shards } else { 1 + shards / 50 };
                    me.dir(p, split, d, acc, |me, p, d, acc| {
                        for i in 0..n {
                            let sz = me.size(3e7, 2.6e8);
                            let v = me.life(born, me.s, sz, cls, owner, 0.0, None);
                            me.file(p, &format!("shard_{i:05}.npy"), d, &v, acc)?;
                        }
                        Ok(())
                    })?;
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    fn raw(&mut self, p: &str, d: u8, acc: &mut Series, bucket: usize) -> Result<()> {
        let sources = self.n(2.0);
        let cls = if bucket == 3 { 2 } else if bucket == 5 { 4 } else { 1 };
        for si in 0..sources {
            let name = format!("source-{si:03}-{}", self.hex(4));
            // History before the window, then one new day per scan.
            let days = 10 + self.below(30) as usize;
            let per = 5 + self.below(60) as usize;
            self.dir(p, &name, d, acc, |me, p, d, acc| {
                me.dir(p, "2026", d, acc, |me, p, d, acc| {
                    for day in 0..days + me.s {
                        let born = day.saturating_sub(days);
                        let t = ts(born) - (days as i64 - day as i64).max(0) * 86_400;
                        let ds = date(t);
                        let (m, dd) = (&ds[5..7], &ds[8..10]);
                        me.dir(p, &format!("{m}-{dd}"), d, acc, |me, p, d, acc| {
                            for i in 0..per {
                                let sz = me.size(1e5, 5e7);
                                let v = me.life(born, me.s, sz, cls, 0, 0.0, None);
                                let name = if me.p(0.05) { format!("PART-{i:05}.JSONL.GZ") } else { format!("part-{i:05}.jsonl.gz") };
                                me.file(p, &name, d, &v, acc)?;
                            }
                            Ok(())
                        })?;
                    }
                    Ok(())
                })
            })?;
        }
        Ok(())
    }

    fn users(&mut self, p: &str, d: u8, acc: &mut Series) -> Result<()> {
        for (ui, u) in OWNERS.iter().enumerate() {
            if self.p(0.4) {
                continue;
            }
            let owner = ui as u8 + 1;
            self.dir(p, u, d, acc, |me, p, d, acc| {
                me.dir(p, "notebooks", d, acc, |me, p, d, acc| {
                    for i in 0..me.n(4.0) {
                        let born = me.born(0.7);
                        let died = if me.p(0.1) { born + 1 + me.below(5) as usize } else { me.s };
                        let sz = me.size(1e4, 5e6);
                        let v = me.life(born, died, sz, 1, owner, 0.05, None);
                        me.file(p, &format!("Untitled{i}.ipynb"), d, &v, acc)?;
                    }
                    let v = me.life(0, me.s, 2_000_000, 1, owner, 0.0, None);
                    me.file(p, "Data.CSV", d, &v, acc)
                })?;
                let projects = me.n(1.0).min(4);
                for pi in 0..projects {
                    let born = me.born(0.8);
                    me.dir(p, &format!("project-{pi}"), d, acc, |me, p, d, acc| {
                        let v = me.life(born, me.s, 4000, 1, owner, 0.0, None);
                        me.file(p, "README.md", d, &v, acc)?;
                        me.dir(p, "node_modules", d, acc, |me, p, d, acc| {
                            for k in 0..20 + me.below(60) as usize {
                                me.dir(p, &format!("pkg-{k:03}"), d, acc, |me, p, d, acc| {
                                    let v = me.life(born, me.s, 900, 1, owner, 0.0, None);
                                    me.file(p, "package.json", d, &v, acc)?;
                                    let v = me.life(born, me.s, 3000, 1, owner, 0.0, None);
                                    me.file(p, "LICENSE", d, &v, acc)?;
                                    for sub in ["lib", "dist"] {
                                        me.dir(p, sub, d, acc, |me, p, d, acc| {
                                            for f in 0..1 + me.below(6) as usize {
                                                let sz = me.size(500.0, 60_000.0);
                                                let v = me.life(born, me.s, sz, 1, owner, 0.0, None);
                                                me.file(p, &format!("m{f}.js"), d, &v, acc)?;
                                            }
                                            Ok(())
                                        })?;
                                    }
                                    Ok(())
                                })?;
                            }
                            Ok(())
                        })
                    })?;
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    fn logs(&mut self, p: &str, d: u8, acc: &mut Series) -> Result<()> {
        for _ in 0..self.n(4.0) {
            let born = self.born(0.5);
            let active = 1 + self.below(8) as usize;
            let owner = 1 + self.below(8) as u8;
            let name = format!("run-{}", self.uniq());
            self.dir(p, &name, d, acc, |me, p, d, acc| {
                for h in 0..1 + me.below(4) {
                    // Rewritten every scan while active, then left alone.
                    let mut v = me.life(born, me.s, 10_000, 1, owner, 0.0, None);
                    if let Some(last) = v.pop() {
                        let stop = (born + active).min(me.s);
                        let mut cur = last;
                        for s in born + 1..stop {
                            cur.vt = s;
                            v.push(cur);
                            cur.vf = s;
                            cur.size += 50_000 + me.below(1_000_000) as i64;
                            cur.mtime = ts(s) - 600;
                        }
                        cur.vt = me.s;
                        v.push(cur);
                    }
                    me.file(p, &format!("events.out.tfevents.{}.host-{h}.v2", ts(born) - 7200), d, &v, acc)?;
                }
                Ok(())
            })?;
        }
        Ok(())
    }

    fn scratch(&mut self, p: &str, d: u8, acc: &mut Series) -> Result<()> {
        for i in 0..self.n(150.0) {
            let born = self.born(0.2);
            let died = born + 1 + self.below(5) as usize;
            let sz = self.size(1e3, 1e9);
            let owner = self.below(9) as u8;
            let v = self.life(born, died, sz, 1, owner, 0.1, None);
            let name = format!("tmp-{i:06}-{}.bin", self.hex(10));
            self.file(p, &name, d, &v, acc)?;
        }
        Ok(())
    }

    pub fn run(stage: &Path, scans: usize, runs: usize, scale: f64, seed: u64) -> Result<Synth> {
        std::fs::create_dir_all(stage.join("base"))?;
        std::fs::create_dir_all(stage.join("runs"))?;
        let k = scans - runs - 1;
        let mut me = Synth { s: scans, k, scale, rng: seed, base: BTreeMap::new(), runs: Vec::new(), stage: stage.to_path_buf(), versions: 0, file_versions: 0, uid: 0 };
        for i in 0..scans {
            me.runs.push(if i > k { Some(Out::new(&stage.join(format!("runs/{}.parquet", date(ts(i)))))?) } else { None });
        }
        let mut root = Series::new(scans);
        for (bi, b) in BUCKETS.iter().enumerate() {
            me.dir("", b, 1, &mut root, |me, p, d, acc| {
                me.dir(p, "checkpoints", d, acc, |me, p, d, acc| me.checkpoints(p, d, acc, bi))?;
                me.dir(p, "tokenized", d, acc, |me, p, d, acc| me.tokenized(p, d, acc, bi))?;
                me.dir(p, "raw", d, acc, |me, p, d, acc| me.raw(p, d, acc, bi))?;
                if bi < 2 {
                    me.dir(p, "users", d, acc, |me, p, d, acc| me.users(p, d, acc))?;
                }
                me.dir(p, "logs", d, acc, |me, p, d, acc| me.logs(p, d, acc))?;
                me.dir(p, "scratch", d, acc, |me, p, d, acc| me.scratch(p, d, acc))
            })?;
        }
        for o in me.base.values_mut() {
            o.flush()?;
        }
        for o in me.runs.iter_mut().flatten() {
            o.flush()?;
        }
        let base = std::mem::take(&mut me.base);
        for (_, o) in base {
            o.w.close()?;
        }
        for o in std::mem::take(&mut me.runs).into_iter().flatten() {
            o.w.close()?;
        }
        let doc = json!({
            "scans": (0..scans).map(|i| json!({"id": date(ts(i)), "ts": ts(i)})).collect::<Vec<_>>(),
            "base_scans": k + 1,
            "versions": me.versions,
            "file_versions": me.file_versions,
            "scale": scale,
            "seed": seed,
        });
        std::fs::write(stage.join("scans.json"), serde_json::to_string_pretty(&doc)? + "\n")?;
        Ok(me)
    }
}

#[cfg(test)]
mod tests {
    use super::date;

    #[test]
    fn dates() {
        assert_eq!([date(super::T0), date(1_785_369_600), date(super::T0 + 31 * 86_400)], ["2026-10-01".to_string(), "2026-07-30".to_string(), "2026-11-01".to_string()]);
    }
}
