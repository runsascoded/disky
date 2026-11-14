//! An interval-store generation on disk (specs/interval-store.md §2.3–2.7): `scans.json`, the newest manifest's runs,
//! and the `path` sort of the base and of each listed run. A URL store is mirrored first (`oracle/fetch.py`), so the
//! loader only ever reads a local generation dir.

use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::util::{err, Result};

/// A version's `vt` while it is open (2106-01-01 UTC), as `static_names.OPEN`.
pub const OPEN: i64 = 4_291_747_200;

#[derive(Clone, Debug, Serialize, serde::Deserialize)]
pub struct ScanInfo {
    pub id: String,
    pub ts: i64,
}

#[derive(Debug)]
pub struct Generation {
    pub dir: PathBuf,
    /// Base scans then each run scan of the newest manifest, by ordinal.
    pub scans: Vec<ScanInfo>,
    /// The newest manifest's file name, when there is one.
    pub manifest: Option<String>,
    /// The `path` sorts to combine: the base's, then each listed run's.
    pub files: Vec<PathBuf>,
}

/// A manifest name as `manifests.ts` `newestManifest` accepts it: `<scan id>[.mNNN].json`.
fn manifest_name(n: &str) -> bool {
    let Some(stem) = n.strip_suffix(".json") else { return false };
    let (id, rev) = match stem.split_once('.') {
        Some((id, rev)) => (id, Some(rev)),
        None => (stem, None),
    };
    let date = |s: &str| {
        let b = s.as_bytes();
        b.len() == 10 && b[4] == b'-' && b[7] == b'-' && b.iter().enumerate().all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit())
    };
    let id_ok = match id.split_once('T') {
        Some((d, hm)) => date(d) && hm.len() == 4 && hm.bytes().all(|c| c.is_ascii_digit()),
        None => date(id),
    };
    let rev_ok = match rev {
        None => true,
        Some(r) => r.len() == 4 && r.starts_with('m') && r[1..].bytes().all(|c| c.is_ascii_digit()),
    };
    id_ok && rev_ok
}

pub fn open(dir: &Path) -> Result<Generation> {
    let scans_doc: serde_json::Value = serde_json::from_slice(&std::fs::read(dir.join("scans.json"))?)?;
    let mut scans: Vec<ScanInfo> = Vec::new();
    for s in scans_doc["scans"].as_array().ok_or("scans.json: no `scans`")? {
        // A scan without a stamp (gcs 2026-08-10) is never asked for.
        if let (Some(id), Some(ts)) = (s["id"].as_str(), s["ts"].as_i64()) {
            scans.push(ScanInfo { id: id.to_string(), ts });
        }
    }
    let mut files = vec![dir.join("served/path.parquet")];
    let mdir = dir.join("manifests");
    let mut manifest = None;
    if mdir.is_dir() {
        let mut names: Vec<String> = std::fs::read_dir(&mdir)?
            .filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned()))
            .filter(|n| manifest_name(n))
            .collect();
        names.sort();
        if let Some(newest) = names.pop() {
            let m: serde_json::Value = serde_json::from_slice(&std::fs::read(mdir.join(&newest))?)?;
            let stamps = &m["stamps"];
            for id in m["scans"].as_array().ok_or("manifest: no `scans`")? {
                let id = id.as_str().ok_or("manifest: a non-string scan")?;
                if scans.iter().any(|s| s.id == id) {
                    continue;
                }
                let ts = stamps[id].as_i64().ok_or_else(|| format!("manifest {newest}: no stamp for {id}"))?;
                scans.push(ScanInfo { id: id.to_string(), ts });
            }
            for run in m["runs"].as_array().ok_or("manifest: no `runs`")? {
                let key = run["key"].as_str().ok_or("manifest: a run without a key")?;
                files.push(dir.join(key).join("served/path.parquet"));
            }
            manifest = Some(newest);
        }
    }
    for f in &files {
        if !f.is_file() {
            return err(format!("missing {}", f.display()));
        }
    }
    if scans.len() >= u16::MAX as usize {
        return err("more scans than a u16 ordinal holds");
    }
    Ok(Generation { dir: dir.to_path_buf(), scans, manifest, files })
}

#[cfg(test)]
mod tests {
    use super::manifest_name;

    #[test]
    fn manifest_names() {
        let got: Vec<bool> = ["2026-08-05.json", "2026-08-05.m001.json", "2026-10-09T1236.json", "2026-10-09T1236.m012.json", "x.json", "2026-08-05.json.tmp", "2026-08-05.m1.json"]
            .iter()
            .map(|n| manifest_name(n))
            .collect();
        assert_eq!(got, vec![true, true, true, true, false, false, false]);
    }
}
