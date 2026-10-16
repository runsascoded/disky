//! `/files/<path>`: a file's bytes for the hosted site's previews, through a
//! Cloudflare Tunnel (spec `laptop-file-tunnel.md`). Off unless configured.
//! Read-only: GET / HEAD, one `Range`, `content-type` by extension. A request
//! must carry a valid Access JWT (`access.rs`), and the path must resolve
//! (after `realpath`: no `..`, no symlink out) to a regular file under `root`.
//! The edge enforces who may ask and the deny-list; this re-checks the root.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Component, Path, PathBuf};

use tiny_http::{Request, Response, StatusCode};

use crate::access::Access;

pub struct Files {
    /// Canonical (`realpath`) store root: `/` for a whole-Mac store.
    pub root: PathBuf,
    pub access: Access,
}

pub const JWT_HEADER: &str = "Cf-Access-Jwt-Assertion";

impl Files {
    pub fn new(root: &Path, access: Access) -> std::io::Result<Self> {
        Ok(Files { root: root.canonicalize()?, access })
    }
}

/// `rel` (already percent-decoded, relative to the store root) → the canonical
/// file under `root`. Refuses `..` / absolute / NUL segments up front, then
/// anything whose `realpath` leaves `root` (a symlink out) or isn't a file.
pub fn resolve(root: &Path, rel: &str) -> Result<PathBuf, (u16, &'static str)> {
    let rel = rel.trim_start_matches('/');
    if rel.is_empty() || rel.contains('\0') {
        return Err((400, "bad path"));
    }
    let p = Path::new(rel);
    if !p.components().all(|c| matches!(c, Component::Normal(_) | Component::CurDir)) {
        return Err((400, "bad path"));
    }
    let real = root.join(p).canonicalize().map_err(|_| (404, "not found"))?;
    if !real.starts_with(root) {
        return Err((403, "outside the store root"));
    }
    if !real.is_file() {
        return Err((404, "not a file"));
    }
    Ok(real)
}

/// A `Range` this can't serve: answered 416.
#[derive(Debug, PartialEq)]
pub struct Unsatisfiable;

/// One `bytes=` range over `len` bytes → inclusive `(start, end)`; `Ok(None)`
/// for no header (the whole file); `Err` for a range that can't be served
/// (unsatisfiable, malformed, or a multi-range: answered 416).
pub fn parse_range(h: Option<&str>, len: u64) -> Result<Option<(u64, u64)>, Unsatisfiable> {
    let Some(h) = h else { return Ok(None) };
    let spec = h.trim().strip_prefix("bytes=").ok_or(Unsatisfiable)?;
    if spec.contains(',') {
        return Err(Unsatisfiable);
    }
    let (a, b) = spec.split_once('-').ok_or(Unsatisfiable)?;
    let (a, b) = (a.trim(), b.trim());
    let range = match (a.is_empty(), b.is_empty()) {
        (true, false) => {
            let n: u64 = b.parse().map_err(|_| Unsatisfiable)?;
            if n == 0 {
                return Err(Unsatisfiable);
            }
            (len.saturating_sub(n), len.checked_sub(1).ok_or(Unsatisfiable)?)
        }
        (false, _) => {
            let start: u64 = a.parse().map_err(|_| Unsatisfiable)?;
            let end = if b.is_empty() { len.saturating_sub(1) } else { b.parse::<u64>().map_err(|_| Unsatisfiable)?.min(len.saturating_sub(1)) };
            if start >= len || end < start {
                return Err(Unsatisfiable);
            }
            (start, end)
        }
        (true, true) => return Err(Unsatisfiable),
    };
    Ok(Some(range))
}

fn content_type(p: &Path) -> &'static str {
    match p.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref().unwrap_or("") {
        "pdf" => "application/pdf",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "heic" => "image/heic",
        "svg" => "image/svg+xml",
        "mp4" | "m4v" => "video/mp4",
        "mov" => "video/quicktime",
        "webm" => "video/webm",
        "mp3" => "audio/mpeg",
        "m4a" => "audio/mp4",
        "wav" => "audio/wav",
        "json" => "application/json",
        "csv" => "text/csv; charset=utf-8",
        "txt" | "md" | "log" | "py" | "rs" | "ts" | "tsx" | "js" | "sh" | "toml" | "yml" | "yaml" => "text/plain; charset=utf-8",
        "html" | "htm" => "text/plain; charset=utf-8",
        "parquet" => "application/vnd.apache.parquet",
        _ => "application/octet-stream",
    }
}

fn h(k: &str, v: &str) -> tiny_http::Header {
    tiny_http::Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap()
}

type Out = Response<Box<dyn Read + Send>>;

fn err(status: u16, msg: &str) -> Out {
    let b = msg.as_bytes().to_vec();
    let n = b.len();
    Response::new(StatusCode(status), vec![h("content-type", "text/plain; charset=utf-8"), h("cache-control", "no-store")], Box::new(std::io::Cursor::new(b)) as Box<dyn Read + Send>, Some(n), None)
}

/// The response for `/files/<rel>` given the request's JWT and `Range` headers.
pub fn respond(files: &Files, rel: &str, jwt: Option<&str>, range: Option<&str>) -> Out {
    let Some(jwt) = jwt else { return err(401, "missing Access JWT") };
    if let Err(e) = files.access.verify(jwt) {
        return err(403, &format!("bad Access JWT: {e}"));
    }
    let path = match resolve(&files.root, rel) {
        Ok(p) => p,
        Err((s, m)) => return err(s, m),
    };
    let Ok(mut f) = File::open(&path) else { return err(403, "unreadable") };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    // `text/html` would render the laptop's files as the tunnel origin's pages: served as text.
    let mut headers = vec![h("content-type", content_type(&path)), h("accept-ranges", "bytes"), h("cache-control", "private, no-store"), h("x-content-type-options", "nosniff")];
    match parse_range(range, len) {
        Ok(None) => Response::new(StatusCode(200), headers, Box::new(f) as Box<dyn Read + Send>, Some(len as usize), None),
        Ok(Some((a, b))) => {
            if f.seek(SeekFrom::Start(a)).is_err() {
                return err(500, "seek failed");
            }
            headers.push(h("content-range", &format!("bytes {a}-{b}/{len}")));
            Response::new(StatusCode(206), headers, Box::new(f.take(b - a + 1)) as Box<dyn Read + Send>, Some((b - a + 1) as usize), None)
        }
        Err(Unsatisfiable) => {
            let mut r = err(416, "range not satisfiable");
            r.add_header(h("content-range", &format!("bytes */{len}")));
            r
        }
    }
}

/// Serve a tiny_http request for `/files/<rel>`.
pub fn handle(files: &Files, rel: &str, req: Request) {
    let get = |name: &str| req.headers().iter().find(|x| x.field.as_str().as_str().eq_ignore_ascii_case(name)).map(|x| x.value.as_str().to_string());
    let (jwt, range) = (get(JWT_HEADER), get("Range"));
    let _ = req.respond(respond(files, rel, jwt.as_deref(), range.as_deref()));
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::access::tests::{access, claims, token, CLIENT};
    use std::os::unix::fs::symlink;

    pub struct Tmp(PathBuf);
    impl Drop for Tmp {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    /// `<tmp>/root/{a.txt, sub/b.pdf, out -> ../secret.txt, in -> sub/b.pdf}` and `<tmp>/secret.txt`.
    pub fn tree(name: &str) -> (Tmp, PathBuf) {
        let base = std::env::temp_dir().join(format!("dt-files-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let root = base.join("root");
        std::fs::create_dir_all(root.join("sub")).unwrap();
        std::fs::write(root.join("a.txt"), b"0123456789").unwrap();
        std::fs::write(root.join("sub/b.pdf"), b"%PDF-1.7").unwrap();
        std::fs::write(base.join("secret.txt"), b"secret").unwrap();
        symlink("../secret.txt", root.join("out")).unwrap();
        symlink("sub/b.pdf", root.join("in")).unwrap();
        let root = root.canonicalize().unwrap();
        (Tmp(base), root)
    }

    #[test]
    fn resolves_under_root_only() {
        let (_t, root) = tree("resolve");
        assert_eq!(resolve(&root, "a.txt"), Ok(root.join("a.txt")));
        assert_eq!(resolve(&root, "/sub/./b.pdf"), Ok(root.join("sub/b.pdf")));
        assert_eq!(resolve(&root, "in"), Ok(root.join("sub/b.pdf")));
        assert_eq!(resolve(&root, "../secret.txt"), Err((400, "bad path")));
        assert_eq!(resolve(&root, "sub/../../secret.txt"), Err((400, "bad path")));
        assert_eq!(resolve(&root, "out"), Err((403, "outside the store root")));
        assert_eq!(resolve(&root, "sub"), Err((404, "not a file")));
        assert_eq!(resolve(&root, "nope"), Err((404, "not found")));
        assert_eq!(resolve(&root, ""), Err((400, "bad path")));
        assert_eq!(resolve(&root, "a\0.txt"), Err((400, "bad path")));
    }

    #[test]
    fn parses_ranges() {
        assert_eq!(parse_range(None, 10), Ok(None));
        assert_eq!(parse_range(Some("bytes=0-3"), 10), Ok(Some((0, 3))));
        assert_eq!(parse_range(Some("bytes=4-"), 10), Ok(Some((4, 9))));
        assert_eq!(parse_range(Some("bytes=-3"), 10), Ok(Some((7, 9))));
        assert_eq!(parse_range(Some("bytes=-30"), 10), Ok(Some((0, 9))));
        assert_eq!(parse_range(Some("bytes=8-100"), 10), Ok(Some((8, 9))));
        assert_eq!(parse_range(Some("bytes=10-"), 10), Err(Unsatisfiable));
        assert_eq!(parse_range(Some("bytes=5-2"), 10), Err(Unsatisfiable));
        assert_eq!(parse_range(Some("bytes=0-1,4-5"), 10), Err(Unsatisfiable));
        assert_eq!(parse_range(Some("bytes=-0"), 10), Err(Unsatisfiable));
        assert_eq!(parse_range(Some("items=0-1"), 10), Err(Unsatisfiable));
        assert_eq!(parse_range(Some("bytes=0-"), 0), Err(Unsatisfiable));
    }

    fn read(r: Out) -> (u16, String) {
        let status = r.status_code().0;
        let mut out = vec![];
        r.into_reader().read_to_end(&mut out).unwrap();
        (status, String::from_utf8_lossy(&out).into_owned())
    }

    fn header(r: &Out, k: &str) -> Option<String> {
        r.headers().iter().find(|x| x.field.as_str().as_str().eq_ignore_ascii_case(k)).map(|x| x.value.as_str().to_string())
    }

    #[test]
    fn serves_with_valid_jwt_only() {
        let (_t, root) = tree("serve");
        let files = Files { root: root.clone(), access: access(Some(CLIENT)) };
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
        let jwt = token("k1", &claims(now));

        let r = respond(&files, "a.txt", None, None);
        assert_eq!(read(r), (401, "missing Access JWT".into()));
        let r = respond(&files, "a.txt", Some("garbage"), None);
        assert_eq!(read(r), (403, "bad Access JWT: not a JWT".into()));
        let mut stale = claims(now);
        stale["exp"] = (now - 3600).into();
        let r = respond(&files, "a.txt", Some(&token("k1", &stale)), None);
        assert_eq!(read(r), (403, "bad Access JWT: expired".into()));

        let r = respond(&files, "a.txt", Some(&jwt), None);
        assert_eq!((header(&r, "content-type"), header(&r, "accept-ranges"), header(&r, "cache-control")), (Some("text/plain; charset=utf-8".into()), Some("bytes".into()), Some("private, no-store".into())));
        assert_eq!(read(r), (200, "0123456789".into()));

        let r = respond(&files, "a.txt", Some(&jwt), Some("bytes=2-5"));
        assert_eq!(header(&r, "content-range"), Some("bytes 2-5/10".into()));
        assert_eq!(read(r), (206, "2345".into()));
        let r = respond(&files, "a.txt", Some(&jwt), Some("bytes=-2"));
        assert_eq!(read(r), (206, "89".into()));
        let r = respond(&files, "a.txt", Some(&jwt), Some("bytes=20-"));
        assert_eq!(header(&r, "content-range"), Some("bytes */10".into()));
        assert_eq!(read(r).0, 416);

        let r = respond(&files, "sub/b.pdf", Some(&jwt), None);
        assert_eq!(header(&r, "content-type"), Some("application/pdf".into()));
        assert_eq!(read(r), (200, "%PDF-1.7".into()));
        assert_eq!(read(respond(&files, "out", Some(&jwt), None)), (403, "outside the store root".into()));
        assert_eq!(read(respond(&files, "../secret.txt", Some(&jwt), None)), (400, "bad path".into()));
    }
}
