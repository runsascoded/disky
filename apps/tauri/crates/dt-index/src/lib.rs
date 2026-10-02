//! disk-tree's path store in Rust (spec `specs/rust-engine.md` phase 4): a
//! layer-1 capture (or a walk fed row by row) → the store's two sorts and the
//! snapshot JSONs that `dt-cloud path-index -g` writes (`cloud/src/dt_cloud/viz.py`
//! `write_path_index` / `_write_store`, `disk_tree/find/tiers.py`), so the
//! site's readers serve either:
//!
//! - `path-index.parquet`: every dir (each ancestor prefix of a file's dir,
//!   descendant-inclusive) and every file, sorted `(depth, path)`;
//!   `path-index-bysize.parquet`: the same rows sorted `(⌊log2 size⌋ desc,
//!   path)`, size 0 last. Zstd, 8K-row groups, `tier` / `sort` (/ `bucket`) in
//!   the key-value metadata. Columns: `path, usr, size, depth, kind, n_files,
//!   n_children, n_desc, mtime, mtime_mean, created, last_read, age_b0..6,
//!   sum_storage_class_id_2..4` (`usr` / `last_read` NULL: no attribution or
//!   access logs on a laptop; class pivots 0).
//! - `meta.json` (`asof`, totals, class bytes, the sorts' rows/groups) and
//!   `age.json` (bytes/objects per (created day, first dir segment)).
//!
//! Not written: the `.groups.json` / `.groups.parquet` footer sidecars (a
//! serverless reader's range-read plan; a local reader has the footer).

use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io;
use std::path::Path;
use std::sync::Arc;

use arrow_array::cast::AsArray;
use arrow_array::types::{Int64Type, TimestampMillisecondType};
use arrow_array::{ArrayRef, Float64Array, Int32Array, Int64Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::arrow_writer::ArrowWriterOptions;
use parquet::arrow::ArrowWriter;
use parquet::basic::{Compression, ZstdLevel};
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;
use serde_json::json;

/// `index.AGE_EDGES_DAYS`: `age_b<i>` holds bytes created under `EDGES[i]`
/// days before the scan day (and at least the previous edge); the last bucket
/// is everything older. A future stamp counts as the newest.
pub const AGE_EDGES_DAYS: [i64; 6] = [1, 7, 30, 91, 365, 1095];
const N_AGE: usize = AGE_EDGES_DAYS.len() + 1;
/// The store's range-read unit (`index.ROW_GROUP_SIZE`).
pub const ROW_GROUP_ROWS: usize = 8192;
pub const PATH_FILE: &str = "path-index.parquet";
pub const BYSIZE_FILE: &str = "path-index-bysize.parquet";

fn err<E: std::fmt::Display>(e: E) -> io::Error {
    io::Error::other(e.to_string())
}

#[derive(Default, Clone)]
struct Agg {
    b: i64,
    o: i64,
    /// Σ size × created (seconds): exact, like the engine's DECIMAL(38,0).
    wts: i128,
    wb: i64,
    mtime: Option<i64>,
    created: Option<i64>,
    age: [i64; N_AGE],
}

impl Agg {
    fn add(&mut self, o: &Agg) {
        self.b += o.b;
        self.o += o.o;
        self.wts += o.wts;
        self.wb += o.wb;
        self.mtime = self.mtime.max(o.mtime);
        self.created = self.created.max(o.created);
        for (a, b) in self.age.iter_mut().zip(o.age) {
            *a += b;
        }
    }
}

/// One store row.
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub path: String,
    pub size: i64,
    pub depth: i32,
    pub dir: bool,
    pub n_files: i64,
    pub n_children: i64,
    pub n_desc: i64,
    pub mtime: i64,
    pub mtime_mean: Option<f64>,
    pub created: Option<i64>,
    pub age: [i64; N_AGE],
}

/// The age bucket of a stamp created on epoch day `day`, at scan day `asof`.
fn age_bucket(asof: i64, day: i64) -> usize {
    let age = asof - day;
    AGE_EDGES_DAYS.iter().position(|&e| age < e).unwrap_or(AGE_EDGES_DAYS.len())
}

fn segments(p: &str) -> usize {
    p.split('/').count()
}

/// Folds listing rows (`bucket`, `name`, size, created) into the store.
pub struct Reducer {
    asof_day: i64,
    /// Per dir (`fp` = bucket[/dir]): its own files' totals (`dir_stats`).
    dirs: HashMap<String, Agg>,
    files: Vec<Row>,
    /// `(created day, no dir, first dir segment)` → (bytes, objects)
    /// (`age.json`): the engine's `ORDER BY ALL` puts a NULL segment (files
    /// directly in the root) after the named ones.
    ages: BTreeMap<(i64, bool, String), (i64, i64)>,
}

impl Reducer {
    /// `asof_day`: the scan's epoch day (the age buckets' "now").
    pub fn new(asof_day: i64) -> Self {
        Reducer { asof_day, dirs: HashMap::new(), files: vec![], ages: BTreeMap::new() }
    }

    /// One file. A capture's `bucket` is its scan root (`/Users/ryan`): the
    /// leading `/` goes, so it tiles like a bucket name; the filesystem root
    /// (`/`, empty once stripped) re-splits each row on its first segment, and
    /// files directly under `/` (no dir to sit in) are dropped.
    pub fn push(&mut self, bucket: &str, name: &str, size: i64, created_ms: i64) {
        let bucket = bucket.trim_start_matches('/');
        let (bucket, name) = if bucket.is_empty() {
            match name.split_once('/') {
                Some(split) => split,
                None => return,
            }
        } else {
            (bucket, name)
        };
        let dir = name.rfind('/').map_or("", |i| &name[..i]);
        let fp = if dir.is_empty() { bucket.to_string() } else { format!("{bucket}/{dir}") };
        let secs = created_ms.div_euclid(1000);
        // `epoch(created)::BIGINT`: DuckDB rounds a double half to even.
        let rounded = (created_ms as f64 / 1000.0).round_ties_even() as i64;
        let day = created_ms.div_euclid(86_400_000);
        let bucket_i = age_bucket(self.asof_day, day);
        let mut age = [0; N_AGE];
        age[bucket_i] = size;
        let a = self.dirs.entry(fp).or_default();
        a.add(&Agg { b: size, o: 1, wts: size as i128 * rounded as i128, wb: size, mtime: Some(secs), created: Some(secs), age });
        let d1 = dir.split('/').next().unwrap().to_string();
        let e = self.ages.entry((day, dir.is_empty(), d1)).or_default();
        e.0 += size;
        e.1 += 1;
        let path = format!("{bucket}/{name}");
        self.files.push(Row {
            depth: segments(&path) as i32,
            path,
            size,
            dir: false,
            n_files: 1,
            n_children: 0,
            n_desc: 0,
            mtime: secs,
            mtime_mean: Some(secs as f64),
            created: Some(secs),
            age,
        });
    }

    /// Every file of the capture dir `dir` (its `shard-*.parquet`).
    pub fn push_capture(&mut self, dir: &Path) -> io::Result<()> {
        let mut shards: Vec<_> = std::fs::read_dir(dir)?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("shard-") && n.ends_with(".parquet")))
            .collect();
        shards.sort();
        for shard in shards {
            let reader = ParquetRecordBatchReaderBuilder::try_new(File::open(&shard)?).map_err(err)?.build().map_err(err)?;
            for batch in reader {
                let batch = batch.map_err(err)?;
                let col = |n: &str| batch.column_by_name(n).ok_or_else(|| err(format!("{}: no `{n}` column", shard.display())));
                let (bucket, name) = (col("bucket")?.as_string::<i64>(), col("name")?.as_string::<i64>());
                let size = col("size_bytes")?.as_primitive::<Int64Type>();
                let created = col("created")?.as_primitive::<TimestampMillisecondType>();
                for i in 0..batch.num_rows() {
                    self.push(bucket.value(i), name.value(i), size.value(i), created.value(i));
                }
            }
        }
        Ok(())
    }

    /// The store's rows (unsorted) and the snapshot's totals.
    pub fn finish(self) -> Store {
        // Every ancestor prefix of every dir, descendant-inclusive (`ptu`).
        let mut ptu: HashMap<String, (i32, Agg)> = HashMap::new();
        for (fp, a) in &self.dirs {
            let mut end = 0;
            for (k, seg) in fp.split('/').enumerate() {
                end += seg.len() + usize::from(k > 0);
                ptu.entry(fp[..end].to_string()).or_insert_with(|| (k as i32 + 1, Agg::default())).1.add(a);
            }
        }
        // `n_children` = own files + direct subdirs; `n_desc` = own files +
        // Σ (subdir's n_desc + 1), folded bottom-up.
        let parent = |p: &str| p.rfind('/').map(|i| p[..i].to_string());
        let mut subdirs: HashMap<String, i64> = HashMap::new();
        for p in ptu.keys() {
            if let Some(q) = parent(p) {
                *subdirs.entry(q).or_default() += 1;
            }
        }
        let mut order: Vec<(&String, i32)> = ptu.iter().map(|(p, (d, _))| (p, *d)).collect();
        order.sort_by(|a, b| b.1.cmp(&a.1));
        let mut below: HashMap<String, i64> = HashMap::new();
        let mut n_desc: HashMap<&String, i64> = HashMap::new();
        for (p, _) in order {
            let own = self.dirs.get(p).map_or(0, |a| a.o);
            let nd = own + below.get(p).copied().unwrap_or(0);
            n_desc.insert(p, nd);
            if let Some(q) = parent(p) {
                *below.entry(q).or_default() += nd + 1;
            }
        }
        let mut rows: Vec<Row> = ptu
            .iter()
            .map(|(p, (depth, a))| Row {
                path: p.clone(),
                size: a.b,
                depth: *depth,
                dir: true,
                n_files: a.o,
                n_children: self.dirs.get(p).map_or(0, |a| a.o) + subdirs.get(p).copied().unwrap_or(0),
                n_desc: n_desc[p],
                mtime: a.mtime.unwrap_or(0),
                mtime_mean: (a.wb > 0).then(|| a.wts as f64 / a.wb as f64),
                created: a.created,
                age: a.age,
            })
            .collect();
        let (total_bytes, total_objects) =
            self.dirs.values().fold((0, 0), |(b, o), a| (b + a.b, o + a.o));
        rows.extend(self.files);
        let ages = self
            .ages
            .into_iter()
            .map(|((d, files, d1), (b, o))| (d, (!files).then_some(d1), b, o))
            .collect::<Vec<_>>();
        Store { rows, total_bytes, total_objects, ages }
    }
}

pub struct Store {
    pub rows: Vec<Row>,
    pub total_bytes: i64,
    pub total_objects: i64,
    /// `(day, d1, bytes, objects)`, ordered `(day, d1)` with no-dir rows last.
    ages: Vec<(i64, Option<String>, i64, i64)>,
}

fn size_bucket(size: i64) -> Option<u32> {
    (size > 0).then(|| 63 - size.leading_zeros())
}

fn schema() -> Arc<Schema> {
    let mut fields = vec![
        Field::new("path", DataType::Utf8, true),
        Field::new("usr", DataType::Utf8, true),
        Field::new("size", DataType::Int64, true),
        Field::new("depth", DataType::Int32, true),
        Field::new("kind", DataType::Utf8, true),
        Field::new("n_files", DataType::Int64, true),
        Field::new("n_children", DataType::Int64, true),
        Field::new("n_desc", DataType::Int64, true),
        Field::new("mtime", DataType::Int64, true),
        Field::new("mtime_mean", DataType::Float64, true),
        Field::new("created", DataType::Int64, true),
        Field::new("last_read", DataType::Int32, true),
    ];
    fields.extend((0..N_AGE).map(|i| Field::new(format!("age_b{i}"), DataType::Int64, true)));
    fields.extend((2..=4).map(|c| Field::new(format!("sum_storage_class_id_{c}"), DataType::Int64, true)));
    Arc::new(Schema::new(fields))
}

fn batch(rows: &[&Row]) -> io::Result<RecordBatch> {
    let n = rows.len();
    let i64s = |f: &dyn Fn(&Row) -> i64| Arc::new(Int64Array::from_iter_values(rows.iter().map(|r| f(r)))) as ArrayRef;
    let mut cols: Vec<ArrayRef> = vec![
        Arc::new(StringArray::from_iter_values(rows.iter().map(|r| r.path.as_str()))),
        Arc::new(StringArray::new_null(n)),
        i64s(&|r| r.size),
        Arc::new(Int32Array::from_iter_values(rows.iter().map(|r| r.depth))),
        Arc::new(StringArray::from_iter_values(rows.iter().map(|r| if r.dir { "dir" } else { "file" }))),
        i64s(&|r| r.n_files),
        i64s(&|r| r.n_children),
        i64s(&|r| r.n_desc),
        i64s(&|r| r.mtime),
        Arc::new(Float64Array::from_iter(rows.iter().map(|r| r.mtime_mean))),
        Arc::new(Int64Array::from_iter(rows.iter().map(|r| r.created))),
        Arc::new(Int32Array::new_null(n)),
    ];
    for i in 0..N_AGE {
        cols.push(i64s(&move |r| r.age[i]));
    }
    for _ in 2..=4 {
        cols.push(Arc::new(Int64Array::from(vec![0i64; n])));
    }
    RecordBatch::try_new(schema(), cols).map_err(err)
}

/// Write `rows` (already in tier order) to `path`; returns the row groups.
fn write_tier(path: &Path, rows: &[&Row], kv: &[(&str, &str)]) -> io::Result<usize> {
    let props = WriterProperties::builder()
        .set_compression(Compression::ZSTD(ZstdLevel::default()))
        .set_max_row_group_row_count(Some(ROW_GROUP_ROWS))
        .set_key_value_metadata(Some(kv.iter().map(|(k, v)| KeyValue::new(k.to_string(), v.to_string())).collect()))
        .build();
    let tmp = path.with_extension("parquet.tmp");
    let opts = ArrowWriterOptions::new().with_properties(props).with_skip_arrow_metadata(true);
    let mut w = ArrowWriter::try_new_with_options(File::create(&tmp)?, schema(), opts).map_err(err)?;
    for chunk in rows.chunks(ROW_GROUP_ROWS) {
        w.write(&batch(chunk)?).map_err(err)?;
        w.flush().map_err(err)?;
    }
    let meta = w.close().map_err(err)?;
    std::fs::rename(&tmp, path)?;
    Ok(meta.num_row_groups())
}

/// Summary of one written sort.
pub struct Sort {
    pub rows: usize,
    pub groups: usize,
}

impl Store {
    /// Write `path-index.parquet` + `path-index-bysize.parquet` into `dir`.
    pub fn write_index(&self, dir: &Path) -> io::Result<[Sort; 2]> {
        std::fs::create_dir_all(dir)?;
        let mut by_path: Vec<&Row> = self.rows.iter().collect();
        by_path.sort_unstable_by(|a, b| a.depth.cmp(&b.depth).then_with(|| a.path.cmp(&b.path)));
        let path_groups = write_tier(&dir.join(PATH_FILE), &by_path, &[("tier", "path"), ("sort", "depth,path,usr")])?;
        // `⌊log2 size⌋` descending, size 0 (no bucket) last, then path.
        let mut by_size = by_path;
        by_size.sort_unstable_by(|a, b| {
            let (x, y) = (size_bucket(a.size), size_bucket(b.size));
            match (x, y) {
                (Some(x), Some(y)) => y.cmp(&x),
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => std::cmp::Ordering::Equal,
            }
            .then_with(|| a.path.cmp(&b.path))
        });
        let size_groups = write_tier(
            &dir.join(BYSIZE_FILE),
            &by_size,
            &[("tier", "bysize"), ("sort", "size_bucket desc,path,usr"), ("bucket", "log2")],
        )?;
        let n = self.rows.len();
        Ok([Sort { rows: n, groups: path_groups }, Sort { rows: n, groups: size_groups }])
    }

    /// Write `meta.json` + `age.json` into `dir`.
    pub fn write_snapshot(&self, dir: &Path, asof: &str, sorts: &[Sort; 2]) -> io::Result<()> {
        std::fs::create_dir_all(dir)?;
        let ages: Vec<_> = self
            .ages
            .iter()
            .map(|(d, d1, b, o)| json!({"d": d, "d1": d1.as_deref().unwrap_or("(files)"), "b": b, "o": o}))
            .collect();
        std::fs::write(dir.join("age.json"), serde_json::to_string(&ages)? + "\n")?;
        let mut class_bytes = serde_json::Map::new();
        if self.total_bytes != 0 {
            class_bytes.insert("1".into(), self.total_bytes.into());
        }
        let [p, s] = sorts;
        let meta = json!({
            "asof": asof,
            "generated": today_local(),
            "published": now_utc_ms(),
            "total_bytes": self.total_bytes,
            "total_objects": self.total_objects,
            "class_bytes": class_bytes,
            "index": {
                "rows": p.rows,
                "sorts": {"path": {"rows": p.rows, "groups": p.groups}, "bysize": {"rows": s.rows, "groups": s.groups}},
            },
        });
        std::fs::write(dir.join("meta.json"), serde_json::to_string_pretty(&meta)? + "\n")
    }
}

/// Broken-down time of epoch seconds `t`, local or UTC.
fn tm(t: i64, local: bool) -> libc::tm {
    unsafe {
        let tt = t as libc::time_t;
        let mut out: libc::tm = std::mem::zeroed();
        if local {
            libc::localtime_r(&tt, &mut out);
        } else {
            libc::gmtime_r(&tt, &mut out);
        }
        out
    }
}

/// `YYYY-MM-DD` (local), as `dt.date.today()`.
fn today_local() -> String {
    let t = tm(now().0, true);
    format!("{:04}-{:02}-{:02}", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday)
}

/// `2026-10-02T15:15:21.173Z`.
fn now_utc_ms() -> String {
    let (secs, ms) = now();
    let t = tm(secs, false);
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{ms:03}Z", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
}

fn now() -> (i64, u32) {
    let d = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap();
    (d.as_secs() as i64, d.subsec_millis())
}

/// Epoch day of `YYYY-MM-DD` (days from civil, proleptic Gregorian).
pub fn epoch_day(date: &str) -> Option<i64> {
    let mut it = date.get(..10)?.split('-');
    let (y, m, d): (i64, i64, i64) = (it.next()?.parse().ok()?, it.next()?.parse().ok()?, it.next()?.parse().ok()?);
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    Some(era * 146_097 + doe - 719_468)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_days() {
        assert_eq!(epoch_day("1970-01-01"), Some(0));
        assert_eq!(epoch_day("2026-10-02"), Some(20728));
        assert_eq!(epoch_day("2020-01-01T00:00"), Some(18262));
    }

    #[test]
    fn age_buckets() {
        assert_eq!([0, 1, 6, 7, 1094, 1095, -3].map(|a| age_bucket(100, 100 - a)), [0, 1, 1, 2, 5, 6, 0]);
    }

    #[test]
    fn folds_a_tree() {
        let mut r = Reducer::new(20728);
        let ms = 20728 * 86_400_000;
        r.push("/", "Users/ryan/a.bin", 2048, ms);
        r.push("/", "Users/ryan/c/z", 1024, ms);
        r.push("/", ".file", 1, ms);
        let s = r.finish();
        let mut rows: Vec<_> = s.rows.iter().map(|r| (r.path.as_str(), r.depth, r.size, r.n_children, r.n_desc)).collect();
        rows.sort();
        assert_eq!(
            rows,
            [
                ("Users", 1, 3072, 1, 4),
                ("Users/ryan", 2, 3072, 2, 3),
                ("Users/ryan/a.bin", 3, 2048, 0, 0),
                ("Users/ryan/c", 3, 1024, 1, 1),
                ("Users/ryan/c/z", 4, 1024, 0, 0),
            ]
        );
        assert_eq!((s.total_bytes, s.total_objects), (3072, 2));
    }
}
