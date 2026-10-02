//! The local read API on loopback: what the site's Pages Functions serve for
//! one store, from a scans dir (`view.rs`), plus the built SPA. Same-origin,
//! so the SPA's relative `/api/*` / `/data/*` calls land here unchanged.
//!
//! - `/data/<store>/scans.json`: scan ids, newest first.
//! - `/data/<store>/<scan>/{meta,age}.json`: the snapshot files.
//! - `/api/subtree`, `/api/series`: `view.rs`; `/api/age-pyramid`: the empty
//!   plan (no pyramid locally; the age lens reads the tree's `ag`).
//! - Any other path: a file of the SPA's build, else its `index.html` (client
//!   routes); other `/api/*`, `/auth/*`: 404.

use std::io::{self, Cursor};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::{json, Value};
use tiny_http::{Header, Method, Request, Response, Server};

use crate::view::{self, Scans, ViewOpts};

pub struct Config {
    pub scans: PathBuf,
    /// The SPA's build (`site/dist`).
    pub web: PathBuf,
    /// The store key in `/data/<store>/` (the SPA's `VITE_STORE`).
    pub store: String,
    /// The store root's name in the tree (the Functions' `ROOT_LABEL`).
    pub root_label: String,
}

type Resp = Response<Cursor<Vec<u8>>>;

fn header(k: &str, v: &str) -> Header {
    Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap()
}

fn body(status: u16, ctype: &str, bytes: Vec<u8>) -> Resp {
    Response::from_data(bytes).with_status_code(status).with_header(header("content-type", ctype)).with_header(header("cache-control", "no-store"))
}

fn json_resp(status: u16, v: &Value) -> Resp {
    body(status, "application/json", serde_json::to_vec(v).unwrap())
}

fn text(status: u16, msg: &str) -> Resp {
    body(status, "text/plain; charset=utf-8", msg.as_bytes().to_vec())
}

fn content_type(p: &Path) -> &'static str {
    match p.extension().and_then(|e| e.to_str()).unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript",
        "css" => "text/css",
        "json" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "wasm" => "application/wasm",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

fn file(p: &Path) -> Option<Resp> {
    let bytes = std::fs::read(p).ok()?;
    Some(body(200, content_type(p), bytes))
}

fn query(url: &str) -> Vec<(String, String)> {
    url.split_once('?')
        .map(|(_, q)| {
            q.split('&')
                .filter(|kv| !kv.is_empty())
                .map(|kv| {
                    let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
                    (decode(k), decode(v))
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Percent-decoding, `+` as space; a malformed escape stays literal.
fn decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let hex = (b[i] == b'%' && i + 2 < b.len()).then(|| std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok())).flatten();
        match (b[i], hex) {
            (_, Some(v)) => {
                out.push(v);
                i += 3;
            }
            (b'+', _) => {
                out.push(b' ');
                i += 1;
            }
            (c, _) => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A number as `JSON.stringify` writes it: integral values without `.0`.
fn js_num(x: f64) -> Value {
    if x.fract() == 0.0 && x.abs() < 9e15 { (x as i64).into() } else { x.into() }
}

fn handle(cfg: &Config, scans: &Scans, url: &str) -> Resp {
    let path = decode(url.split('?').next().unwrap_or("/"));
    let q = query(url);
    let get = |k: &str| q.iter().find(|(n, _)| n == k).map(|(_, v)| v.as_str());
    let num = |k: &str, d: f64| get(k).and_then(|v| v.parse::<f64>().ok()).filter(|v| *v != 0.0).unwrap_or(d);
    let data = format!("/data/{}/", cfg.store);
    if let Some(rest) = path.strip_prefix(&data) {
        if rest == "scans.json" {
            return json_resp(200, &json!(scans.list()));
        }
        return match rest.split_once('/') {
            Some((id, name)) if view::is_scan_id(id) && matches!(name, "meta.json" | "age.json") => {
                file(&scans.dir.join(id).join(name)).unwrap_or_else(|| text(404, "not found"))
            }
            _ => text(404, "not found"),
        };
    }
    match path.as_str() {
        "/api/subtree" => {
            let date = get("date").unwrap_or("");
            let p = get("path").unwrap_or("").trim_end_matches('/').to_string();
            if !view::is_scan_id(date) {
                return text(400, "bad date");
            }
            if p.contains("..") || p.starts_with('/') {
                return text(400, "bad path");
            }
            const QUANT: f64 = 128.0;
            let (w, h) = ((num("w", 1280.0) / QUANT).ceil() * QUANT, (num("h", 800.0) / QUANT).ceil() * QUANT);
            let (min_area, atten) = (num("minArea", 12.0), num("atten", 2.0));
            let max_depth = get("depth").and_then(|v| v.parse().ok()).filter(|d| *d != 0);
            let scan = match scans.get(date) {
                Ok(s) => s,
                Err(_) => return text(404, "no such scan"),
            };
            let o = ViewOpts { path: &p, w, h, min_area, atten, max_depth, root_label: &cfg.root_label };
            match view::subtree(&scan, &o) {
                Ok(Some(v)) => {
                    let mut out = serde_json::Map::new();
                    out.extend([("date".into(), date.into()), ("path".into(), p.clone().into()), ("w".into(), js_num(w)), ("h".into(), js_num(h)), ("minArea".into(), js_num(min_area)), ("atten".into(), js_num(atten))]);
                    out.extend(v);
                    json_resp(200, &out.into())
                }
                Ok(None) => text(404, "path not found"),
                Err(e) => text(500, &format!("subtree failed: {e}")),
            }
        }
        "/api/series" => {
            let p = get("path").unwrap_or("").trim_end_matches('/').to_string();
            if p.contains("..") || p.starts_with('/') {
                return json_resp(400, &json!({"error": "bad path"}));
            }
            match view::series(scans, &p, get("split") == Some("roots")) {
                Ok(v) => json_resp(200, &v),
                Err(e) => json_resp(500, &json!({"error": e.to_string()})),
            }
        }
        "/api/age-pyramid" => {
            let budget = get("bin_budget").and_then(|v| v.parse::<i64>().ok());
            json_resp(200, &json!({"records": [], "plan": {"outputBin": "1d", "tier": "1d", "binBudget": budget}}))
        }
        p if p.starts_with("/api/") || p.starts_with("/auth/") || p.starts_with("/data/") || p.starts_with("/v1/") => text(404, "not served locally"),
        p => {
            let rel = p.trim_start_matches('/');
            let f = cfg.web.join(rel);
            if !rel.is_empty() && !rel.split('/').any(|s| s == "..") && f.is_file() {
                file(&f).unwrap()
            } else {
                file(&cfg.web.join("index.html")).unwrap_or_else(|| text(404, "no SPA build"))
            }
        }
    }
}

fn respond(cfg: &Config, scans: &Scans, req: Request) {
    let resp = if *req.method() == Method::Get || *req.method() == Method::Head { handle(cfg, scans, req.url()) } else { text(405, "read-only") };
    let _ = req.respond(resp);
}

/// Serve on `addr` (e.g. `127.0.0.1:7792`) with `threads` workers; blocks.
pub fn serve(cfg: Config, addr: &str, threads: usize) -> io::Result<()> {
    let server = Arc::new(Server::http(addr).map_err(|e| io::Error::other(e.to_string()))?);
    let cfg = Arc::new(cfg);
    let scans = Arc::new(Scans::new(cfg.scans.clone()));
    let workers: Vec<_> = (0..threads.max(1))
        .map(|_| {
            let (server, cfg, scans) = (server.clone(), cfg.clone(), scans.clone());
            std::thread::spawn(move || {
                for req in server.incoming_requests() {
                    respond(&cfg, &scans, req);
                }
            })
        })
        .collect();
    for w in workers {
        let _ = w.join();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_queries() {
        assert_eq!(query("/api/subtree?path=Users%2Fryan&q=a+b&w=1280"), [("path".into(), "Users/ryan".into()), ("q".into(), "a b".into()), ("w".into(), "1280".into())]);
        assert_eq!(decode("100%"), "100%");
        assert_eq!(decode("%C3%BC"), "ü");
    }
}
