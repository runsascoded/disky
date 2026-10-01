//! Where a capture's files go: a local dir, or an object store URL —
//! `r2://bucket/prefix` (the bucket's endpoint + profile, as `blobfs.fs_for`),
//! `s3://bucket/prefix` (ambient AWS credentials) or `file:///dir` (the store
//! code path, offline). Files are written whole (one PUT each): shards are
//! bounded by `batch_rows`, far below a single PUT's 5 GiB.

use std::io;
use std::path::PathBuf;
use std::sync::Arc;

use object_store::aws::AmazonS3Builder;
use object_store::local::LocalFileSystem;
use object_store::path::Path as Key;
use object_store::{ObjectStore, ObjectStoreExt, PutPayload};

use crate::creds;

pub enum Target {
    Dir(PathBuf),
    Store {
        /// The URL as given, sans trailing `/` (what paths are printed under).
        url: String,
        /// The key prefix within the store (`""` at the bucket root).
        prefix: String,
        store: Arc<dyn ObjectStore>,
        rt: Arc<tokio::runtime::Runtime>,
    },
}

/// `(scheme, bucket, prefix)` for `scheme://bucket/prefix…`.
fn split_url(url: &str) -> Option<(&str, &str, &str)> {
    let (scheme, rest) = url.split_once("://")?;
    let (bucket, prefix) = rest.split_once('/').unwrap_or((rest, ""));
    Some((scheme, bucket, prefix.trim_matches('/')))
}

impl Target {
    /// Resolve `to`, failing (bad scheme, no endpoint, no credentials) before
    /// any walk, as the Python CLI does.
    pub fn parse(to: &str) -> Result<Target, String> {
        if !to.contains("://") {
            return Ok(Target::Dir(PathBuf::from(to)));
        }
        let url = to.trim_end_matches('/').to_string();
        let (scheme, bucket, prefix) = split_url(&url).ok_or_else(|| format!("bad URL {to:?}"))?;
        let store: Arc<dyn ObjectStore> = match scheme {
            "file" => {
                // `file:///abs/dir`: the bucket segment is empty, the prefix the path.
                let dir = PathBuf::from(format!("/{bucket}{}{prefix}", if prefix.is_empty() { "" } else { "/" }));
                std::fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
                let store = LocalFileSystem::new_with_prefix(&dir).map_err(|e| e.to_string())?;
                return Ok(Target::Store { url, prefix: String::new(), store: Arc::new(store), rt: runtime()? });
            }
            "r2" | "s3" => {
                let yml = creds::buckets_yml()?;
                let mut b = AmazonS3Builder::new().with_bucket_name(bucket);
                let creds = if scheme == "r2" {
                    let ep = creds::r2_endpoint(&yml, bucket).ok_or_else(|| {
                        format!("r2://{bucket}: no endpoint — set {}, or give the bucket an `endpoint_url` in buckets.yml", creds::R2_ENDPOINT_VAR)
                    })?;
                    b = b.with_endpoint(ep).with_region("auto");
                    creds::resolve(creds::bucket_field(&yml, bucket, "profile").as_deref())?
                } else {
                    let c = creds::resolve(None)?;
                    b = b.with_region(c.region.clone().unwrap_or_else(|| "us-east-1".into()));
                    c
                };
                b = b.with_access_key_id(creds.key_id).with_secret_access_key(creds.secret);
                if let Some(t) = creds.token {
                    b = b.with_token(t);
                }
                Arc::new(b.build().map_err(|e| e.to_string())?)
            }
            other => return Err(format!("unsupported --to scheme {other:?} (r2://, s3://, file:// or a local dir)")),
        };
        Ok(Target::Store { url: url.clone(), prefix: prefix.to_string(), store, rt: runtime()? })
    }

    /// The target `rel` (a `/`-joined relative path) below this one.
    pub fn join(&self, rel: &str) -> Target {
        match self {
            Target::Dir(d) => Target::Dir(d.join(rel)),
            Target::Store { url, prefix, store, rt } => Target::Store {
                url: format!("{url}/{rel}"),
                prefix: if prefix.is_empty() { rel.to_string() } else { format!("{prefix}/{rel}") },
                store: store.clone(),
                rt: rt.clone(),
            },
        }
    }

    /// Make the target exist (a dir; a no-op on a store, whose keys need no parents).
    pub fn create(&self) -> io::Result<()> {
        match self {
            Target::Dir(d) => std::fs::create_dir_all(d),
            Target::Store { .. } => Ok(()),
        }
    }

    /// Write `name` under the target, whole.
    pub fn put(&self, name: &str, data: Vec<u8>) -> io::Result<()> {
        match self {
            Target::Dir(d) => std::fs::write(d.join(name), data),
            Target::Store { prefix, store, rt, .. } => {
                let key = if prefix.is_empty() { name.to_string() } else { format!("{prefix}/{name}") };
                let key = Key::parse(&key).map_err(io::Error::other)?;
                rt.block_on(store.put(&key, PutPayload::from(data))).map(|_| ()).map_err(io::Error::other)
            }
        }
    }

    /// What to print: the dir, or the URL.
    pub fn display(&self) -> String {
        match self {
            Target::Dir(d) => d.display().to_string(),
            Target::Store { url, .. } => url.clone(),
        }
    }
}

fn runtime() -> Result<Arc<tokio::runtime::Runtime>, String> {
    tokio::runtime::Builder::new_current_thread().enable_all().build().map(Arc::new).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_urls() {
        assert_eq!(
            [split_url("r2://bk/a/b"), split_url("s3://bk"), split_url("file:///x/y"), split_url("nope")],
            [Some(("r2", "bk", "a/b")), Some(("s3", "bk", "")), Some(("file", "", "x/y")), None],
        );
    }
}
