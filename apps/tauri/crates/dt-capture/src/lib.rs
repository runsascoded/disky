//! `capture` in Rust (spec `specs/rust-engine.md`): walk a local tree in-process
//! with `dt-walker` and write the layer-1 capture the Python CLI writes
//! (`disk_tree/cli/capture.py`), so `disk-tree reduce` and the Batch ingest
//! read either:
//!
//! - `<to>/<host>/<root slug>/<YYYY-MM-DDTHH-MM-SSZ>/shard-NNNNN.parquet`: files
//!   only (dirs are implied by the engines; APFS dirs hold 0 blocks), columns
//!   `bucket` (the root), `name` (path relative to the root), `size_bytes`
//!   (blocks × 512), `created` (mtime, ms, UTC), `storage_class_id` (0);
//!   snappy, 64K-row groups, at most `batch_rows` rows per shard.
//! - `_SUCCESS.json`: `{format, version, scheme, root, host, time, n_rows,
//!   n_shards, error_count, error_paths[, container]}`.
//!
//! `--to` is a local dir or an object-store URL (`target.rs`): `r2://` with the
//! bucket's endpoint and profile, `s3://`, or `file://`.

pub mod apfs;
pub mod creds;
pub mod target;

use std::io;
use std::sync::Arc;

use arrow_array::{ArrayRef, Int64Array, LargeStringArray, RecordBatch, TimestampMillisecondArray};
use arrow_schema::{DataType, Field, Schema, TimeUnit};
use parquet::arrow::ArrowWriter;
use parquet::basic::Compression;
use parquet::file::properties::WriterProperties;
use serde_json::json;

pub const FORMAT: &str = "disk-tree-capture";
pub const VERSION: u32 = 1;
pub const MARKER: &str = "_SUCCESS.json";
pub const ROW_GROUP_ROWS: usize = 1 << 16;

pub struct Opts {
    pub root: String,
    pub to: target::Target,
    pub host: String,
    pub batch_rows: usize,
    pub one_fs: bool,
    /// Record the APFS container in the manifest (macOS).
    pub container: bool,
    /// Called after each shard is written, with the files and bytes so far.
    pub progress: Option<Box<dyn Fn(u64, u64)>>,
}

pub struct Summary {
    /// The capture dir: a path, or a URL under `--to`'s.
    pub dir: String,
    pub n_rows: u64,
    /// Σ `size_bytes`.
    pub n_bytes: u64,
    pub n_shards: u64,
    pub error_count: u64,
}

/// `<root>` with no trailing slash, except `/` itself.
pub fn normalize_root(root: &str) -> String {
    let r = root.trim_end_matches('/');
    if r.is_empty() { "/".into() } else { r.into() }
}

/// The capture dir's root segment: `/Users/ryan` → `Users__ryan`, `/` → `root`.
pub fn slug(root: &str) -> String {
    let s = root.trim_matches('/').replace('/', "__");
    if s.is_empty() { "root".into() } else { s }
}

fn schema() -> Arc<Schema> {
    Arc::new(Schema::new(vec![
        Field::new("bucket", DataType::LargeUtf8, true),
        Field::new("name", DataType::LargeUtf8, true),
        Field::new("size_bytes", DataType::Int64, true),
        Field::new("created", DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".into())), true),
        Field::new("storage_class_id", DataType::Int64, true),
    ]))
}

/// UTC broken-down time of epoch seconds.
fn utc(t: i64) -> libc::tm {
    unsafe {
        let tt = t as libc::time_t;
        let mut tm: libc::tm = std::mem::zeroed();
        libc::gmtime_r(&tt, &mut tm);
        tm
    }
}

/// `(stamp dir name, ISO 8601 like Python's `datetime.isoformat()` in UTC)`.
pub fn stamps(secs: i64, micros: u32) -> (String, String) {
    let t = utc(secs);
    let (y, mo, d, h, mi, s) = (t.tm_year + 1900, t.tm_mon + 1, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec);
    (
        format!("{y:04}-{mo:02}-{d:02}T{h:02}-{mi:02}-{s:02}Z"),
        format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{micros:06}+00:00"),
    )
}

struct Shards {
    dir: target::Target,
    root: String,
    names: Vec<String>,
    sizes: Vec<i64>,
    mtimes: Vec<i64>,
    n_rows: u64,
    n_bytes: u64,
    n_shards: u64,
}

impl Shards {
    fn flush(&mut self) -> io::Result<()> {
        if self.names.is_empty() {
            return Ok(());
        }
        let n = self.names.len();
        self.n_bytes += self.sizes.iter().sum::<i64>() as u64;
        let cols: Vec<ArrayRef> = vec![
            Arc::new(LargeStringArray::from(vec![self.root.as_str(); n])),
            Arc::new(LargeStringArray::from(std::mem::take(&mut self.names))),
            Arc::new(Int64Array::from(std::mem::take(&mut self.sizes))),
            Arc::new(TimestampMillisecondArray::from(std::mem::take(&mut self.mtimes)).with_timezone("UTC")),
            Arc::new(Int64Array::from(vec![0i64; n])),
        ];
        let batch = RecordBatch::try_new(schema(), cols).map_err(io::Error::other)?;
        let props = WriterProperties::builder()
            .set_compression(Compression::SNAPPY)
            .set_max_row_group_row_count(Some(ROW_GROUP_ROWS))
            .build();
        let mut buf = Vec::new();
        let mut w = ArrowWriter::try_new(&mut buf, schema(), Some(props)).map_err(io::Error::other)?;
        w.write(&batch).map_err(io::Error::other)?;
        w.close().map_err(io::Error::other)?;
        self.dir.put(&format!("shard-{:05}.parquet", self.n_shards), buf)?;
        self.n_rows += n as u64;
        self.n_shards += 1;
        Ok(())
    }
}

/// Capture `opts.root` under `opts.to`; returns the capture dir and counts.
pub fn capture(opts: &Opts) -> io::Result<Summary> {
    let root = normalize_root(&opts.root);
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap();
    let (stamp, iso) = stamps(now.as_secs() as i64, now.subsec_micros());
    let dir = opts.to.join(&format!("{}/{}/{stamp}", opts.host, slug(&root)));
    dir.create()?;

    let excludes = dt_walker::default_excludes();
    let mut walker = dt_walker::Walker::new(&excludes).one_fs(opts.one_fs);
    let mut shards = Shards { dir, root: root.clone(), names: vec![], sizes: vec![], mtimes: vec![], n_rows: 0, n_bytes: 0, n_shards: 0 };
    let prefix: Vec<u8> = if root == "/" { b"/".to_vec() } else { format!("{root}/").into_bytes() };
    let batch_rows = opts.batch_rows.max(1);
    let mut errbuf: Vec<u8> = Vec::new();
    walker.walk_records(
        root.as_bytes(),
        |r| {
            // Directories (and the root itself) aren't rows: the engines imply dirs.
            if r.kind == b'd' || !r.path.starts_with(&prefix) {
                return Ok(());
            }
            shards.names.push(String::from_utf8_lossy(&r.path[prefix.len()..]).into_owned());
            shards.sizes.push((r.blocks * 512) as i64);
            shards.mtimes.push(r.mtime * 1000);
            if shards.names.len() >= batch_rows {
                shards.flush()?;
                if let Some(f) = &opts.progress {
                    f(shards.n_rows, shards.n_bytes);
                }
            }
            Ok(())
        },
        &mut errbuf,
    )?;
    shards.flush()?;

    let error_paths: Vec<String> = walker.errors.paths.iter().map(|p| String::from_utf8_lossy(p).into_owned()).collect();
    let mut manifest = json!({
        "format": FORMAT,
        "version": VERSION,
        "scheme": "file",
        "root": root,
        "host": opts.host,
        "time": iso,
        "n_rows": shards.n_rows,
        "n_shards": shards.n_shards,
        "error_count": walker.errors.count,
        "error_paths": error_paths,
    });
    if opts.container {
        match apfs::container_for(&root) {
            Ok(c) => manifest["container"] = serde_json::to_value(c).unwrap(),
            // An annotation: a non-APFS root legitimately has none, and a
            // diskutil failure shouldn't cost the walk its manifest.
            Err(e) => eprintln!("{root}: no APFS container recorded: {e}"),
        }
    }
    shards.dir.put(MARKER, (serde_json::to_string_pretty(&manifest)? + "\n").into_bytes())?;
    Ok(Summary { dir: shards.dir.display(), n_rows: shards.n_rows, n_bytes: shards.n_bytes, n_shards: shards.n_shards, error_count: walker.errors.count })
}

/// `DISK_TREE_HOST`, else the hostname (what `capture.py` uses).
pub fn host() -> String {
    if let Ok(h) = std::env::var("DISK_TREE_HOST") {
        return h;
    }
    let mut buf = [0u8; 256];
    let ok = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len()) } == 0;
    let end = buf.iter().position(|&b| b == 0).unwrap_or(0);
    if ok { String::from_utf8_lossy(&buf[..end]).into_owned() } else { "unknown".into() }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs_and_roots() {
        assert_eq!([slug("/"), slug("/Users/ryan"), slug("/Users/ryan/")], ["root", "Users__ryan", "Users__ryan"]);
        assert_eq!([normalize_root("/"), normalize_root("/a/b/"), normalize_root("//")], ["/", "/a/b", "/"]);
    }

    #[test]
    fn stamps_match_python() {
        // 2026-09-30T23:54:03.012345Z
        assert_eq!(
            stamps(1_790_812_443, 12_345),
            ("2026-09-30T23-54-03Z".to_string(), "2026-09-30T23:54:03.012345+00:00".to_string())
        );
    }
}
