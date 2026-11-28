//! The repo's interval-store fixtures (`site/functions/_lib/fixtures/iv/interval-store/g*`) as slim packs, the way
//! `fsb pack-slim` writes them (load, `SlimIndex::from_index`, write), each pinned by a digest of its bytes
//! (`tests/golden/slim-packs.json`; the header's one timing, `stats.build_s`, left out). Each fixture is packed as
//! committed (class columns `c2`..`c4`) and as a copy under gcs's names (`nearline_bytes`, `coldline_bytes`,
//! `archive_bytes`): both give the same bytes.

use std::collections::BTreeMap;
use std::fs::File;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::RecordBatch;
use arrow_schema::{Field, Schema};
use full_scan::load::load;
use full_scan::slim::SlimIndex;
use full_scan::store;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use serde_json::Value;
use sha2::{Digest, Sha256};

const GCS_NAMES: [(&str, &str); 3] = [("c2", "nearline_bytes"), ("c3", "coldline_bytes"), ("c4", "archive_bytes")];

fn fixtures() -> Vec<PathBuf> {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../site/functions/_lib/fixtures/iv/interval-store");
    let mut v: Vec<PathBuf> = std::fs::read_dir(&dir).unwrap().map(|e| e.unwrap().path()).filter(|p| p.is_dir()).collect();
    v.sort();
    assert_eq!(v.len(), 9, "{}: the nine fixture generations", dir.display());
    v
}

/// A fresh scratch dir under cargo's per-target tmp.
fn scratch(name: &str) -> PathBuf {
    let d = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// `src` copied to `dst`, every `path.parquet` rewritten with its columns renamed (`ren`), row group by row group.
fn renamed_copy(src: &Path, dst: &Path, ren: &[(&str, &str)]) {
    std::fs::create_dir_all(dst).unwrap();
    for e in std::fs::read_dir(src).unwrap() {
        let p = e.unwrap().path();
        let to = dst.join(p.file_name().unwrap());
        if p.is_dir() {
            renamed_copy(&p, &to, ren);
        } else if p.file_name().unwrap() == "path.parquet" {
            rename_columns(&p, &to, ren);
        } else {
            std::fs::copy(&p, &to).unwrap();
        }
    }
}

fn rename_columns(src: &Path, dst: &Path, ren: &[(&str, &str)]) {
    let b = ParquetRecordBatchReaderBuilder::try_new(File::open(src).unwrap()).unwrap();
    let groups = b.metadata().num_row_groups();
    let fields: Vec<Field> = b
        .schema()
        .fields()
        .iter()
        .map(|f| {
            let name = ren.iter().find(|(a, _)| *a == f.name().as_str()).map_or(f.name().as_str(), |(_, b)| *b);
            f.as_ref().clone().with_name(name)
        })
        .collect();
    let schema = Arc::new(Schema::new(fields));
    let mut w = ArrowWriter::try_new(File::create(dst).unwrap(), schema.clone(), None).unwrap();
    for g in 0..groups {
        let r = ParquetRecordBatchReaderBuilder::try_new(File::open(src).unwrap()).unwrap().with_row_groups(vec![g]).build().unwrap();
        for batch in r {
            w.write(&RecordBatch::try_new(schema.clone(), batch.unwrap().columns().to_vec()).unwrap()).unwrap();
        }
        w.flush().unwrap();
    }
    w.close().unwrap();
}

/// A pack's bytes, digested: the magic, the header (less `meta.stats.build_s`, a timing) and every section.
fn digest(pack: &Path) -> String {
    let b = std::fs::read(pack).unwrap();
    let hl = u64::from_le_bytes(b[8..16].try_into().unwrap()) as usize;
    let mut h: Value = serde_json::from_slice(&b[16..16 + hl]).unwrap();
    let first = h["sections"].as_array().unwrap().iter().map(|s| s["offset"].as_u64().unwrap()).min().unwrap() as usize;
    h["meta"]["stats"].as_object_mut().unwrap().remove("build_s").expect("the build's time");
    let mut d = Sha256::new();
    d.update(&b[..8]);
    d.update(serde_json::to_vec(&h).unwrap());
    d.update(&b[first..]);
    hex::encode(d.finalize())
}

/// `gen`'s slim pack, written to `out`: its digest.
fn slim_pack(gen: &Path, out: &Path) -> String {
    let idx = load(&store::open(gen).unwrap()).unwrap();
    SlimIndex::from_index(&idx).unwrap().write(out).unwrap();
    digest(out)
}

#[test]
fn fixture_slim_packs() {
    let tmp = scratch("fixture-slim-packs");
    let mut got: BTreeMap<String, String> = BTreeMap::new();
    for g in fixtures() {
        let n = g.file_name().unwrap().to_string_lossy().into_owned();
        let plain = slim_pack(&g, &tmp.join(format!("{n}.fsb")));
        let gcs = tmp.join(format!("{n}-gcs"));
        renamed_copy(&g, &gcs, &GCS_NAMES);
        assert_eq!(slim_pack(&gcs, &tmp.join(format!("{n}-gcs.fsb"))), plain, "{n}: under gcs's class column names");
        got.insert(n, plain);
    }
    let golden = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/slim-packs.json");
    let want: BTreeMap<String, String> = match std::fs::read(&golden) {
        Ok(b) => serde_json::from_slice(&b).unwrap(),
        Err(_) => panic!("{} missing; this tree's digests:\n{}", golden.display(), serde_json::to_string_pretty(&got).unwrap()),
    };
    assert_eq!(got, want);
}
