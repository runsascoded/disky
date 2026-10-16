//! A connected Mac's uploads (spec `device-enrollment.md` phase 3; the API is
//! m3's `specs/device-api.md`): no bucket key on the host. Each file is PUT to
//! a presigned URL that the site mints for this host's device token:
//! `POST <site>/api/hosts/<id>/uploads {root, stamp, keys}` → `{urls, expires}`,
//! landing at `captures/<id>/<root>/<stamp>/<key>`. Files go up in the order the
//! capture writes them, so `_SUCCESS.json` (whose event starts the ingest) is
//! last, after every shard's PUT succeeded.

use std::io;
use std::thread::sleep;
use std::time::Duration;

use serde_json::{json, Value};

/// A host enrolled with a site: where to ask for URLs, and as whom.
pub struct Device {
    /// The site's origin, e.g. `https://disk.rbw.sh` (no trailing `/`).
    pub site: String,
    /// The host id (= its store key) the site assigned at enrollment.
    pub id: String,
    /// The device token (a `host:<id>` grant); never logged.
    pub token: String,
}

/// Attempts per request: transient failures (connection, 5xx, 429) back off 2, 4, 8 s.
const TRIES: u32 = 4;

/// The upload API's root segment: the capture's root slug with every byte
/// outside `[A-Za-z0-9._-]` mapped to `-`, at most 64 bytes.
pub fn key_segment(slug: &str) -> String {
    let s: String = slug.bytes().map(|b| if b.is_ascii_alphanumeric() || b"._-".contains(&b) { b as char } else { '-' }).take(64).collect();
    if s.is_empty() { "root".into() } else { s }
}

impl Device {
    fn uploads_url(&self) -> String {
        format!("{}/api/hosts/{}/uploads", self.site.trim_end_matches('/'), self.id)
    }

    /// The presigned PUT URL for `key` under `root`/`stamp`.
    fn sign(&self, root: &str, stamp: &str, key: &str) -> io::Result<String> {
        let body = json!({"root": root, "stamp": stamp, "keys": [key]}).to_string();
        let resp = retry(|| {
            ureq::post(&self.uploads_url()).set("Authorization", &format!("Bearer {}", self.token)).set("Content-Type", "application/json").send_string(&body)
        })?;
        let v: Value = serde_json::from_str(&resp.into_string()?).map_err(io::Error::other)?;
        v.get("urls").and_then(|u| u.get(key)).and_then(Value::as_str).map(str::to_string).ok_or_else(|| io::Error::other(format!("uploads: no URL for {key} in the response")))
    }

    /// Upload `data` as `key` under `root`/`stamp`. An expired URL (403) is
    /// signed again once.
    pub fn put(&self, root: &str, stamp: &str, key: &str, data: &[u8]) -> io::Result<()> {
        for fresh in [false, true] {
            let url = self.sign(root, stamp, key)?;
            match retry(|| ureq::put(&url).send_bytes(data)) {
                Ok(_) => return Ok(()),
                Err(e) if !fresh && e.to_string().contains("HTTP 403") => continue,
                Err(e) => return Err(e),
            }
        }
        unreachable!()
    }
}

/// `f` until it succeeds or fails for good; an HTTP failure reads `HTTP <code>: <body>`.
fn retry(f: impl Fn() -> Result<ureq::Response, ureq::Error>) -> io::Result<ureq::Response> {
    let mut wait = Duration::from_secs(2);
    let mut attempt = 1;
    loop {
        let (msg, transient) = match f() {
            Ok(r) => return Ok(r),
            Err(ureq::Error::Status(401, _)) => ("the device token was refused: this Mac is disconnected (Settings → Connect)".to_string(), false),
            Err(ureq::Error::Status(code, r)) => (format!("HTTP {code}: {}", r.into_string().unwrap_or_default().trim()), code == 429 || code >= 500),
            Err(e) => (e.to_string(), true),
        };
        if !transient || attempt == TRIES {
            return Err(io::Error::other(msg));
        }
        sleep(wait);
        wait *= 2;
        attempt += 1;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    #[test]
    fn key_segments() {
        assert_eq!(
            [key_segment("root"), key_segment("Users__ryan"), key_segment("Volumes__My Drive"), key_segment("Volumes__Café"), key_segment("")],
            ["root", "Users__ryan", "Volumes__My-Drive", "Volumes__Caf--", "root"],
        );
        assert_eq!(key_segment(&"a".repeat(80)).len(), 64);
    }

    /// One recorded request: method, path, auth header, body.
    type Seen = Arc<Mutex<Vec<(String, String, Option<String>, String)>>>;

    /// A fake site + bucket: `/api/hosts/<id>/uploads` answers with URLs back
    /// to `/put/<root>/<stamp>/<key>`, which stores the body. `status` forces
    /// the uploads route's answer (e.g. 401).
    /// What the fake bucket stored: `<root>/<stamp>/<key>` → bytes.
    type Puts = Arc<Mutex<BTreeMap<String, Vec<u8>>>>;

    fn fake(status: Option<u16>) -> (String, Seen, Puts) {
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let base = format!("http://{}", server.server_addr().to_ip().unwrap());
        let (seen, puts): (Seen, Puts) = Default::default();
        let (s2, p2, b2) = (seen.clone(), puts.clone(), base.clone());
        std::thread::spawn(move || {
            for mut req in server.incoming_requests() {
                let mut body = vec![];
                req.as_reader().read_to_end(&mut body).unwrap();
                let auth = req.headers().iter().find(|h| h.field.as_str().as_str().eq_ignore_ascii_case("authorization")).map(|h| h.value.to_string());
                let (method, path) = (req.method().to_string(), req.url().to_string());
                s2.lock().unwrap().push((method.clone(), path.clone(), auth, String::from_utf8_lossy(&body).into()));
                let resp = if let Some(rest) = path.strip_prefix("/put/") {
                    p2.lock().unwrap().insert(rest.to_string(), body);
                    tiny_http::Response::from_string("")
                } else if let Some(code) = status {
                    tiny_http::Response::from_string("{\"error\":\"revoked\"}").with_status_code(code)
                } else {
                    let v: Value = serde_json::from_slice(&body).unwrap();
                    let (root, stamp) = (v["root"].as_str().unwrap(), v["stamp"].as_str().unwrap());
                    let urls: serde_json::Map<String, Value> = v["keys"].as_array().unwrap().iter().map(|k| {
                        let k = k.as_str().unwrap();
                        (k.to_string(), json!(format!("{b2}/put/{root}/{stamp}/{k}")))
                    }).collect();
                    tiny_http::Response::from_string(json!({"urls": urls, "expires": 900}).to_string())
                };
                let _ = req.respond(resp);
            }
        });
        (base, seen, puts)
    }

    #[test]
    fn signs_then_puts() {
        let (site, seen, puts) = fake(None);
        let d = Device { site: site.clone(), id: "m1".into(), token: "tok".into() };
        d.put("root", "2026-10-16T18-00-00Z", "shard-00000.parquet", b"abc").unwrap();
        d.put("root", "2026-10-16T18-00-00Z", "_SUCCESS.json", b"{}").unwrap();
        let seen: Vec<_> = seen.lock().unwrap().iter().map(|(m, p, a, b)| (m.clone(), p.clone(), a.clone(), b.clone())).collect();
        let sign = |k: &str| ("POST".to_string(), "/api/hosts/m1/uploads".to_string(), Some("Bearer tok".to_string()), json!({"root": "root", "stamp": "2026-10-16T18-00-00Z", "keys": [k]}).to_string());
        let put = |k: &str, b: &str| ("PUT".to_string(), format!("/put/root/2026-10-16T18-00-00Z/{k}"), None, b.to_string());
        assert_eq!(seen, [sign("shard-00000.parquet"), put("shard-00000.parquet", "abc"), sign("_SUCCESS.json"), put("_SUCCESS.json", "{}")]);
        assert_eq!(
            puts.lock().unwrap().clone().into_iter().collect::<Vec<_>>(),
            [("root/2026-10-16T18-00-00Z/_SUCCESS.json".to_string(), b"{}".to_vec()), ("root/2026-10-16T18-00-00Z/shard-00000.parquet".to_string(), b"abc".to_vec())],
        );
    }

    #[test]
    fn revoked_token_says_disconnected() {
        let (site, seen, puts) = fake(Some(401));
        let d = Device { site, id: "m1".into(), token: "old".into() };
        let e = d.put("root", "2026-10-16T18-00-00Z", "shard-00000.parquet", b"abc").unwrap_err();
        assert_eq!(e.to_string(), "the device token was refused: this Mac is disconnected (Settings → Connect)");
        assert_eq!((seen.lock().unwrap().len(), puts.lock().unwrap().len()), (1, 0));
    }

    #[test]
    fn captures_through_device() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../target/test-tmp/device-capture");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("a b")).unwrap();
        std::fs::write(dir.join("a b/x.txt"), "hello").unwrap();
        std::fs::write(dir.join("y.txt"), "hi").unwrap();
        let (site, seen, puts) = fake(None);
        let dev = Arc::new(Device { site, id: "m1".into(), token: "tok".into() });
        let opts = crate::Opts {
            root: dir.join("a b").to_string_lossy().into(),
            to: crate::target::Target::Device { dev, rel: String::new() },
            host: "m1".into(),
            batch_rows: 1,
            one_fs: false,
            container: false,
            progress: None,
        };
        let s = crate::capture(&opts).unwrap();
        assert_eq!((s.n_rows, s.n_shards), (1, 1));
        let keys: Vec<String> = seen.lock().unwrap().iter().filter(|r| r.0 == "PUT").map(|r| r.1.rsplit('/').next().unwrap().to_string()).collect();
        assert_eq!(keys, ["shard-00000.parquet", "_SUCCESS.json"]);
        let roots: Vec<String> = puts.lock().unwrap().keys().map(|k| k.split('/').next().unwrap().to_string()).collect();
        let want = key_segment(&crate::slug(&dir.join("a b").to_string_lossy()));
        assert_eq!(roots, [want.clone(), want.clone()]);
    }
}
