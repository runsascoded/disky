//! S3/R2 configuration the Python engine reads, resolved the same way:
//! `buckets.yml` (`endpoint_url` / `profile`, per bucket else `defaults`),
//! `DISK_TREE_R2_ENDPOINT_URL`, and the AWS shared credentials/config files
//! (`object_store` reads env keys only, not profiles).

use std::collections::HashMap;
use std::path::PathBuf;

pub const R2_ENDPOINT_VAR: &str = "DISK_TREE_R2_ENDPOINT_URL";

fn home() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}

/// `$DISK_TREE_ROOT`, else `~/.config/disk-tree` (`disk_tree.config.ROOT_DIR`).
pub fn config_root() -> PathBuf {
    match std::env::var_os("DISK_TREE_ROOT") {
        Some(r) => PathBuf::from(r),
        None => home().join(".config").join("disk-tree"),
    }
}

/// A per-bucket field from `buckets.yml`: the matching `buckets[…].<field>`
/// (truthy only), else `defaults.<field>` (`blobfs._bucket_field`).
pub fn bucket_field(yml: &serde_yaml::Value, bucket: &str, field: &str) -> Option<String> {
    let truthy = |v: Option<&serde_yaml::Value>| v.and_then(|v| v.as_str()).filter(|s| !s.is_empty()).map(String::from);
    if let Some(entries) = yml.get("buckets").and_then(|b| b.as_sequence()) {
        for e in entries {
            let netloc = e.get("uri").and_then(|u| u.as_str()).and_then(|u| u.split_once("://")).map(|(_, rest)| rest.split('/').next().unwrap_or(""));
            if netloc == Some(bucket) {
                if let Some(v) = truthy(e.get(field)) {
                    return Some(v);
                }
            }
        }
    }
    truthy(yml.get("defaults").and_then(|d| d.get(field)))
}

/// The parsed `buckets.yml` (`Null` when absent).
pub fn buckets_yml() -> Result<serde_yaml::Value, String> {
    let p = config_root().join("buckets.yml");
    match std::fs::read_to_string(&p) {
        Ok(s) => serde_yaml::from_str(&s).map_err(|e| format!("{}: {e}", p.display())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(serde_yaml::Value::Null),
        Err(e) => Err(format!("{}: {e}", p.display())),
    }
}

/// `DISK_TREE_R2_ENDPOINT_URL`, else the bucket's `endpoint_url` (`blobfs.r2_endpoint`).
pub fn r2_endpoint(yml: &serde_yaml::Value, bucket: &str) -> Option<String> {
    std::env::var(R2_ENDPOINT_VAR).ok().filter(|s| !s.is_empty()).or_else(|| bucket_field(yml, bucket, "endpoint_url"))
}

/// One `[section]` of an INI file (the AWS shared files' dialect: `key = value`,
/// `#`/`;` comments). `None` when the file or section is absent.
pub fn ini_section(text: &str, section: &str) -> Option<HashMap<String, String>> {
    let mut found = None;
    let mut cur: Option<String> = None;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if let Some(name) = line.strip_prefix('[').and_then(|l| l.strip_suffix(']')) {
            cur = Some(name.trim().to_string());
            if cur.as_deref() == Some(section) {
                found.get_or_insert_with(HashMap::new);
            }
            continue;
        }
        if cur.as_deref() == Some(section) {
            if let Some((k, v)) = line.split_once('=') {
                found.get_or_insert_with(HashMap::new).insert(k.trim().to_string(), v.trim().to_string());
            }
        }
    }
    found
}

#[derive(Debug, PartialEq)]
pub struct Creds {
    pub key_id: String,
    pub secret: String,
    pub token: Option<String>,
    /// The profile's `region` (`~/.aws/config`), if any.
    pub region: Option<String>,
}

/// Credentials as botocore resolves them: an explicit profile (a bucket's
/// `profile:`) wins; else `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`; else the
/// shared files' `AWS_PROFILE` (or `default`) section.
pub fn resolve(profile: Option<&str>) -> Result<Creds, String> {
    let env = |k: &str| std::env::var(k).ok().filter(|s| !s.is_empty());
    if profile.is_none() {
        if let (Some(key_id), Some(secret)) = (env("AWS_ACCESS_KEY_ID"), env("AWS_SECRET_ACCESS_KEY")) {
            return Ok(Creds { key_id, secret, token: env("AWS_SESSION_TOKEN"), region: env("AWS_REGION").or_else(|| env("AWS_DEFAULT_REGION")) });
        }
    }
    let name = profile.map(String::from).or_else(|| env("AWS_PROFILE")).unwrap_or_else(|| "default".into());
    let creds_path = env("AWS_SHARED_CREDENTIALS_FILE").map(PathBuf::from).unwrap_or_else(|| home().join(".aws").join("credentials"));
    let config_path = env("AWS_CONFIG_FILE").map(PathBuf::from).unwrap_or_else(|| home().join(".aws").join("config"));
    let read = |p: &PathBuf| std::fs::read_to_string(p).unwrap_or_default();
    let config_section = if name == "default" { "default".to_string() } else { format!("profile {name}") };
    let config = ini_section(&read(&config_path), &config_section).unwrap_or_default();
    // Keys may sit in either file; `credentials` wins.
    let mut merged = config.clone();
    merged.extend(ini_section(&read(&creds_path), &name).unwrap_or_default());
    match (merged.get("aws_access_key_id"), merged.get("aws_secret_access_key")) {
        (Some(k), Some(s)) => Ok(Creds {
            key_id: k.clone(),
            secret: s.clone(),
            token: merged.get("aws_session_token").cloned(),
            region: env("AWS_REGION").or_else(|| env("AWS_DEFAULT_REGION")).or_else(|| config.get("region").cloned()),
        }),
        _ => Err(format!("AWS profile {name:?}: no aws_access_key_id/aws_secret_access_key in {} or {}", creds_path.display(), config_path.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ini_sections() {
        let text = "# c\n[default]\naws_access_key_id = A\n\n[m3]\naws_access_key_id=B\naws_secret_access_key = s=3\n[profile x]\nregion = auto\n";
        let get = |s: &str| ini_section(text, s).map(|m| { let mut v: Vec<_> = m.into_iter().collect(); v.sort(); v });
        assert_eq!(
            [get("default"), get("m3"), get("profile x"), get("nope")],
            [
                Some(vec![("aws_access_key_id".into(), "A".into())]),
                Some(vec![("aws_access_key_id".into(), "B".into()), ("aws_secret_access_key".into(), "s=3".into())]),
                Some(vec![("region".into(), "auto".into())]),
                None,
            ],
        );
    }

    #[test]
    fn bucket_fields() {
        let yml: serde_yaml::Value = serde_yaml::from_str(
            "defaults:\n  endpoint_url: https://d\n  profile: dp\nbuckets:\n  - uri: r2://bk/sub\n    profile: bp\n    endpoint_url: ''\n  - uri: s3://other\n",
        ).unwrap();
        assert_eq!(
            [
                bucket_field(&yml, "bk", "profile"),
                bucket_field(&yml, "bk", "endpoint_url"),
                bucket_field(&yml, "other", "profile"),
                bucket_field(&serde_yaml::Value::Null, "bk", "profile"),
            ],
            [Some("bp".into()), Some("https://d".into()), Some("dp".into()), None],
        );
    }
}
