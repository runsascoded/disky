//! `vmd`, VM mode's query server (specs/vm-mode.md §4–5): every tenant's slim pack mapped, each view answered whole
//! (`POST /v1/view`: its shards' partial answers merged) or as one shard's partial answer (`POST /v1/shard`), every
//! call signed under the tenant's own key.
//!
//! Tenants hot-swap: a watcher re-reads the config and each tenant's pack file(s) every few seconds; a changed pack is
//! mapped and faulted in beside the old one, then swapped in, while the old one keeps answering requests pinned to it.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicU64;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::cell::Cell;
use crate::merge::merge;
use crate::pack::Pack;
use crate::query::{Expr, OwnerSel, Query};
use crate::shard::{Floor, ShardAnswer};
use crate::slim::SlimIndex;
use crate::slim_scan::{compile, shard_pass, Touched};
use crate::util::{err, Result};

type HmacSha256 = Hmac<Sha256>;

/// A request's signature may be this many seconds from the server's clock.
pub const SKEW_S: i64 = 60;

#[derive(Clone, Debug, Deserialize)]
pub struct TenantConf {
    pub name: String,
    /// One pack (split into `shards` logical shards), or a physical shard pack per shard (`fsb shard`).
    #[serde(default)]
    pub pack: Option<PathBuf>,
    #[serde(default)]
    pub packs: Vec<PathBuf>,
    #[serde(default = "one")]
    pub shards: usize,
    /// The env var holding the tenant's key (shared with its Worker's `VM_KEY`).
    pub key_env: String,
    /// The deployment's search applies the hex-run rule (`static-hex-runs`), which `vmd` doesn't: refuse searches.
    #[serde(default)]
    pub hex_runs: bool,
    /// Fault the pack in before serving it (default true).
    #[serde(default = "yes")]
    pub prefault: bool,
}

fn one() -> usize {
    1
}
fn yes() -> bool {
    true
}

#[derive(Clone, Debug, Deserialize)]
pub struct Conf {
    #[serde(default = "listen")]
    pub listen: String,
    #[serde(default)]
    pub threads: Option<usize>,
    /// HTTP handler threads (each request's pass runs on the shared pool).
    #[serde(default = "handlers")]
    pub handlers: usize,
    /// Seconds between checks of the config and the packs.
    #[serde(default = "watch")]
    pub watch_s: u64,
    pub tenants: Vec<TenantConf>,
}

fn listen() -> String {
    "127.0.0.1:7340".into()
}
fn handlers() -> usize {
    4
}
fn watch() -> u64 {
    5
}

impl Conf {
    pub fn read(p: &Path) -> Result<Conf> {
        let c: Conf = serde_json::from_slice(&std::fs::read(p)?)?;
        for t in &c.tenants {
            if t.pack.is_some() == !t.packs.is_empty() {
                return err(format!("tenant {}: give `pack` or `packs`", t.name));
            }
            if !t.packs.is_empty() && t.shards != 1 && t.shards != t.packs.len() {
                return err(format!("tenant {}: `shards` is the number of `packs`", t.name));
            }
        }
        Ok(c)
    }
}

/// A generation's pins, as a pack records them (`fsb pack-slim`) and a request names them.
#[derive(Clone, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
pub struct Pins {
    #[serde(default)]
    pub gen: String,
    #[serde(default)]
    pub manifest: String,
}

/// One tenant's packs, mapped and opened.
pub struct Loaded {
    pub pins: Pins,
    /// A whole pack (logical shards) or one per physical shard.
    pub parts: Vec<SlimIndex>,
    pub shards: usize,
    pub loaded_at: i64,
    pub bytes: u64,
    pub load_s: f64,
    /// Each pack file's (path, length, mtime): what the watcher compares.
    pub stamp: Vec<(PathBuf, u64, i64)>,
    pub conf: TenantConf,
}

impl Loaded {
    pub fn open(t: &TenantConf, threads: usize) -> Result<Loaded> {
        let t0 = Instant::now();
        let files: Vec<PathBuf> = match &t.pack {
            Some(p) => vec![p.clone()],
            None => t.packs.clone(),
        };
        let mut parts = Vec::new();
        let mut bytes = 0;
        let mut pins: Option<Pins> = None;
        for f in &files {
            let p = Pack::open(f)?;
            if t.prefault {
                p.prefault(threads)?;
            }
            bytes += p.len();
            let ix = SlimIndex::read(&p)?;
            let pi: Pins = serde_json::from_value(ix.stats.get("pins").cloned().unwrap_or(json!({})))?;
            match &pins {
                Some(x) if x != &pi => return err(format!("tenant {}: its packs hold different generations", t.name)),
                _ => pins = Some(pi),
            }
            parts.push(ix);
        }
        Ok(Loaded {
            pins: pins.unwrap_or_default(),
            shards: if files.len() > 1 { files.len() } else { t.shards.max(1) },
            parts,
            loaded_at: now(),
            bytes,
            load_s: t0.elapsed().as_secs_f64(),
            stamp: stamps(&files),
            conf: t.clone(),
        })
    }

    fn scans(&self) -> Vec<String> {
        self.parts[0].scans.iter().map(|s| s.id.clone()).collect()
    }
}

fn stamps(files: &[PathBuf]) -> Vec<(PathBuf, u64, i64)> {
    files
        .iter()
        .map(|f| {
            let m = std::fs::metadata(f).ok();
            let mt = m.as_ref().and_then(|m| m.modified().ok()).and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0, |d| d.as_nanos() as i64);
            (f.clone(), m.map_or(0, |m| m.len()), mt)
        })
        .collect()
}

pub fn now() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs() as i64
}

#[derive(Default)]
pub struct TenantState {
    pub current: Option<Arc<Loaded>>,
    /// A new pack being mapped and faulted in (its files), while `current` keeps answering.
    pub loading: bool,
    pub error: Option<String>,
}

/// The server's shared state.
pub struct Server {
    pub conf_path: PathBuf,
    pub tenants: RwLock<HashMap<String, TenantState>>,
    pub pool: rayon::ThreadPool,
    pub threads: usize,
}

/// A matcher of the Worker's `QueryAst` (`site/functions/_lib/queryAst.ts`).
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Matcher {
    pub kind: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub start: bool,
    #[serde(default)]
    pub end: bool,
}

/// `(OR of AND-groups) AND NOT (any negative)`; an empty group is true.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct QueryAst {
    pub alts: Vec<Vec<Matcher>>,
    #[serde(default)]
    pub neg: Vec<Matcher>,
}

/// The `/v1/view` and `/v1/shard` body (specs/vm-mode.md §4.2).
#[derive(Clone, Debug, Deserialize)]
pub struct ViewReq {
    pub pins: Pins,
    #[serde(default)]
    pub scan: Option<String>,
    #[serde(default)]
    pub from: Option<String>,
    #[serde(default)]
    pub to: Option<String>,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub q: Option<QueryAst>,
    #[serde(default)]
    pub owners: Option<OwnerSel>,
    #[serde(default)]
    pub classes: Option<Vec<u8>>,
    #[serde(default)]
    pub staged: Option<Vec<String>>,
    pub cell: Cell,
    #[serde(default)]
    pub deadline_ms: Option<u64>,
    /// Also answer in the bench's flat form (`answer`), for the parity checks.
    #[serde(default)]
    pub flat: bool,
    /// `/v1/shard`: which shard, and its floor.
    #[serde(default)]
    pub shard: Option<usize>,
    #[serde(default)]
    pub floor: Option<Floor>,
}

/// A request outside the language (`422`).
pub struct Unsupported(pub String);

fn matcher(m: &Matcher) -> std::result::Result<Expr, Unsupported> {
    if m.kind != "sub" {
        return Err(Unsupported(format!("a `{}` matcher (not in the indexed-only language)", m.kind)));
    }
    let t = format!("{}{}{}", if m.start { "^" } else { "" }, m.text.to_lowercase(), if m.end { "$" } else { "" });
    Ok(if m.text.contains('/') { Expr::P(t) } else { Expr::T(t) })
}

/// The Worker's AST as the engine's expression (specs/vm-mode.md §4.4).
pub fn expr_of(a: &QueryAst) -> std::result::Result<Option<Expr>, Unsupported> {
    let and = |g: &[Matcher]| -> std::result::Result<Expr, Unsupported> {
        let xs = g.iter().map(matcher).collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(if xs.len() == 1 { xs.into_iter().next().unwrap() } else { Expr::And(xs) })
    };
    let pos = if a.alts.is_empty() || a.alts.iter().any(|g| g.is_empty()) {
        None
    } else {
        let gs = a.alts.iter().map(|g| and(g)).collect::<std::result::Result<Vec<_>, _>>()?;
        Some(if gs.len() == 1 { gs.into_iter().next().unwrap() } else { Expr::Or(gs) })
    };
    let neg = if a.neg.is_empty() {
        None
    } else {
        let ns = a.neg.iter().map(matcher).collect::<std::result::Result<Vec<_>, _>>()?;
        Some(Expr::Not(Box::new(if ns.len() == 1 { ns.into_iter().next().unwrap() } else { Expr::Or(ns) })))
    };
    Ok(match (pos, neg) {
        (None, None) => None,
        (Some(p), None) => Some(p),
        (None, Some(n)) => Some(n),
        (Some(p), Some(n)) => Some(Expr::And(vec![p, n])),
    })
}

/// An HTTP answer: status, JSON body, extra headers.
pub struct Reply {
    pub status: u16,
    pub body: Value,
    pub headers: Vec<(String, String)>,
}

fn reply(status: u16, body: Value) -> Reply {
    Reply { status, body, headers: Vec::new() }
}

fn error(status: u16, code: &str, message: impl Into<String>) -> Reply {
    reply(status, json!({"error": code, "message": message.into()}))
}

pub fn sha256_hex(b: &[u8]) -> String {
    hex::encode(Sha256::digest(b))
}

/// The request's signed string (specs/vm-mode.md §4.3).
pub fn request_string(method: &str, path: &str, tenant: &str, ts: &str, body: &[u8]) -> String {
    format!("v1\n{method}\n{path}\n{tenant}\n{ts}\n{}", sha256_hex(body))
}

pub fn response_string(tenant: &str, ts: &str, body: &[u8]) -> String {
    format!("v1-resp\n{tenant}\n{ts}\n{}", sha256_hex(body))
}

pub fn sign(key: &[u8], s: &str) -> String {
    let mut m = HmacSha256::new_from_slice(key).expect("any key length");
    m.update(s.as_bytes());
    hex::encode(m.finalize().into_bytes())
}

fn verify(key: &[u8], s: &str, sig_hex: &str) -> bool {
    let Ok(sig) = hex::decode(sig_hex) else { return false };
    let mut m = HmacSha256::new_from_slice(key).expect("any key length");
    m.update(s.as_bytes());
    m.verify_slice(&sig).is_ok()
}

fn key_of(t: &TenantConf) -> Option<Vec<u8>> {
    std::env::var(&t.key_env).ok().filter(|k| !k.is_empty()).map(|k| k.into_bytes())
}

impl Server {
    pub fn new(conf_path: PathBuf) -> Result<Arc<Server>> {
        let conf = Conf::read(&conf_path)?;
        let threads = conf.threads.unwrap_or_else(|| std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1));
        let pool = rayon::ThreadPoolBuilder::new().num_threads(threads).build()?;
        // Tenants load once the server listens (`run`): each `loading` (503) until its pack is mapped and faulted in.
        Ok(Arc::new(Server { conf_path, tenants: RwLock::new(HashMap::new()), pool, threads }))
    }

    /// Map tenant `t`'s pack(s) and swap them in; errors are kept for `status`.
    fn load(&self, t: TenantConf) {
        {
            let mut ts = self.tenants.write().unwrap();
            let e = ts.entry(t.name.clone()).or_default();
            e.loading = true;
        }
        eprintln!("vmd: loading {}", t.name);
        let r = Loaded::open(&t, self.threads);
        let mut ts = self.tenants.write().unwrap();
        let e = ts.entry(t.name.clone()).or_default();
        e.loading = false;
        match r {
            Ok(l) => {
                eprintln!("vmd: {} loaded: {:?}, {} B, {:.1} s", t.name, l.pins, l.bytes, l.load_s);
                e.current = Some(Arc::new(l));
                e.error = None;
            }
            Err(x) => {
                eprintln!("vmd: {} failed to load: {x}", t.name);
                e.error = Some(x.to_string());
            }
        }
    }

    /// One pass of the watcher: tenants added, removed, or whose pack files changed.
    pub fn watch_once(self: &Arc<Server>) {
        let conf = match Conf::read(&self.conf_path) {
            Ok(c) => c,
            Err(e) => {
                eprintln!("vmd: config unreadable, keeping the last: {e}");
                return;
            }
        };
        let names: Vec<String> = conf.tenants.iter().map(|t| t.name.clone()).collect();
        self.tenants.write().unwrap().retain(|k, _| names.contains(k));
        for t in conf.tenants {
            let changed = {
                let ts = self.tenants.read().unwrap();
                match ts.get(&t.name) {
                    None => true,
                    Some(s) if s.loading => false,
                    Some(s) => match &s.current {
                        None => s.error.is_none(),
                        Some(l) => {
                            let files: Vec<PathBuf> = t.pack.clone().map_or(t.packs.clone(), |p| vec![p]);
                            l.stamp != stamps(&files) || l.conf.shards != t.shards
                        }
                    },
                }
            };
            if changed {
                // Marked before the thread starts, so the next pass never loads it twice.
                self.tenants.write().unwrap().entry(t.name.clone()).or_default().loading = true;
                let me = self.clone();
                std::thread::spawn(move || me.load(t));
            }
        }
    }

    pub fn run(self: &Arc<Server>) -> Result<()> {
        let conf = Conf::read(&self.conf_path)?;
        let http = Arc::new(tiny_http::Server::http(&conf.listen).map_err(|e| e.to_string())?);
        eprintln!("vmd: listening on {} ({} threads)", conf.listen, self.threads);
        self.watch_once();
        {
            let me = self.clone();
            let every = Duration::from_secs(conf.watch_s.max(1));
            std::thread::spawn(move || loop {
                std::thread::sleep(every);
                me.watch_once();
            });
        }
        let mut hs = Vec::new();
        for _ in 0..conf.handlers.max(1) {
            let (me, http) = (self.clone(), http.clone());
            hs.push(std::thread::spawn(move || loop {
                let Ok(mut rq) = http.recv() else { break };
                let mut body = Vec::new();
                let _ = rq.as_reader().read_to_end(&mut body);
                let hdr = |n: &str| rq.headers().iter().find(|h| h.field.to_string().eq_ignore_ascii_case(n)).map(|h| h.value.as_str().to_string());
                let (tenant, ts, sig) = (hdr("x-disky-tenant"), hdr("x-disky-ts"), hdr("x-disky-sig"));
                let method = rq.method().as_str().to_uppercase();
                let path = rq.url().split('?').next().unwrap_or("").to_string();
                let (r, key) = me.handle(&method, &path, tenant.as_deref(), ts.as_deref(), sig.as_deref(), &body);
                let out = serde_json::to_vec(&r.body).unwrap();
                let mut resp = tiny_http::Response::from_data(out.clone()).with_status_code(r.status);
                resp.add_header(tiny_http::Header::from_bytes("content-type", "application/json").unwrap());
                for (k, v) in &r.headers {
                    resp.add_header(tiny_http::Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap());
                }
                if let (Some(k), Some(t), Some(ts)) = (key, tenant.as_deref(), ts.as_deref()) {
                    resp.add_header(tiny_http::Header::from_bytes("x-disky-sig", sign(&k, &response_string(t, ts, &out)).as_bytes()).unwrap());
                }
                let _ = rq.respond(resp);
            }));
        }
        for h in hs {
            let _ = h.join();
        }
        Ok(())
    }

    /// Route and authenticate one request: the reply, and the tenant's key when the request was signed with it (the
    /// reply is then signed too).
    pub fn handle(&self, method: &str, path: &str, tenant: Option<&str>, ts: Option<&str>, sig: Option<&str>, body: &[u8]) -> (Reply, Option<Vec<u8>>) {
        if path == "/healthz" {
            return (reply(200, json!("ok")), None);
        }
        let Some(tenant) = tenant else { return (error(401, "bad-signature", "no x-disky-tenant"), None) };
        let conf = match Conf::read(&self.conf_path) {
            Ok(c) => c,
            Err(e) => return (error(500, "config", e.to_string()), None),
        };
        let Some(tc) = conf.tenants.iter().find(|t| t.name == tenant) else { return (error(404, "unknown-tenant", tenant), None) };
        let Some(key) = key_of(tc) else { return (error(503, "no-key", format!("${} unset", tc.key_env)), None) };
        let (Some(ts), Some(sig)) = (ts, sig) else { return (error(401, "bad-signature", "x-disky-ts and x-disky-sig required"), None) };
        if !verify(&key, &request_string(method, path, tenant, ts, body), sig) {
            return (error(401, "bad-signature", "signature mismatch"), None);
        }
        match ts.parse::<i64>() {
            Ok(t) if (now() - t).abs() <= SKEW_S => {}
            _ => return (error(401, "stale", format!("x-disky-ts {ts}: more than {SKEW_S} s from the server's clock")), None),
        }
        let r = match (method, path) {
            ("GET", "/v1/status") => self.status(tenant),
            ("POST", "/v1/view") | ("POST", "/v1/shard") => self.view(tenant, body, path == "/v1/shard"),
            ("POST", "/v1/merge") => merge_req(tenant, body),
            _ => error(404, "not-found", path),
        };
        (r, Some(key))
    }

    fn status(&self, tenant: &str) -> Reply {
        let ts = self.tenants.read().unwrap();
        let Some(s) = ts.get(tenant) else { return error(404, "unknown-tenant", tenant) };
        let state = match (&s.current, s.loading) {
            (_, true) => "loading",
            (Some(_), false) => "hot",
            (None, false) => "error",
        };
        let cur = s.current.as_ref().map(|l| json!({"pins": l.pins, "scans": l.scans().len(), "newest": l.scans().last(), "loaded_at": l.loaded_at, "load_s": l.load_s, "pack_bytes": l.bytes, "shards": l.shards}));
        reply(200, json!({"tenant": tenant, "state": state, "current": cur, "error": s.error}))
    }

    fn view(&self, tenant: &str, body: &[u8], one_shard: bool) -> Reply {
        let t0 = Instant::now();
        let rq: ViewReq = match serde_json::from_slice(body) {
            Ok(r) => r,
            Err(e) => return error(400, "bad-request", e.to_string()),
        };
        let (l, loading) = {
            let ts = self.tenants.read().unwrap();
            match ts.get(tenant) {
                None => return error(503, "loading", "not loaded yet"),
                Some(s) => (s.current.clone(), s.loading),
            }
        };
        let Some(l) = l.filter(|l| l.pins == rq.pins) else {
            let have = {
                let ts = self.tenants.read().unwrap();
                ts.get(tenant).and_then(|s| s.current.as_ref().map(|l| l.pins.clone()))
            };
            if loading {
                let mut r = error(503, "loading", "a new pack is loading");
                r.headers.push(("retry-after".into(), "10".into()));
                return r;
            }
            let mut r = error(409, "pins", "the loaded pack's pins differ");
            r.body["have"] = json!(have);
            return r;
        };
        if l.conf.hex_runs && rq.q.is_some() {
            return error(422, "unsupported", "this deployment's search applies the hex-run rule, which vmd doesn't");
        }
        let q = match rq.q.as_ref().map(expr_of).transpose() {
            Ok(q) => q.flatten(),
            Err(Unsupported(m)) => return error(422, "unsupported", m),
        };
        let query = Query {
            id: String::new(),
            scan: rq.scan.clone(),
            from: rq.from.clone(),
            to: rq.to.clone(),
            path: rq.path.trim_end_matches('/').to_string(),
            q,
            owners: rq.owners.clone(),
            classes: rq.classes.clone(),
            staged: rq.staged.clone(),
            depth: 0,
            min_ppm: 0,
            cell: Some(rq.cell),
            dirs: false,
            tags: Vec::new(),
        };
        let (ga, gb) = (AtomicU64::new(0), AtomicU64::new(0));
        let mut answers: Vec<ShardAnswer> = Vec::new();
        let mut touched = Touched::default();
        let mut cut = None;
        let t_pass = Instant::now();
        // Which shards this request answers: one (`/v1/shard`), or all of them.
        let which: Vec<usize> = match (one_shard, rq.shard) {
            (true, Some(i)) if i < l.shards => vec![i],
            (true, _) => return error(400, "bad-request", format!("`shard`: 0..{}", l.shards)),
            (false, _) => (0..l.shards).collect(),
        };
        let mut shared = None;
        for i in which {
            let (ix, range) = if l.parts.len() > 1 {
                (&l.parts[i], (0, l.parts[i].n_o()))
            } else {
                let n = l.parts[0].n_o();
                (&l.parts[0], (n * i / l.shards, n * (i + 1) / l.shards))
            };
            // Logical shards share one pack: its query (the search's dictionary pass, the staged spans) once.
            if l.parts.len() > 1 || shared.is_none() {
                shared = Some(match self.pool.install(|| compile(ix, &query, self.threads)) {
                    Ok(c) => c,
                    Err(e) => return error(400, "bad-request", e.to_string()),
                });
            }
            let c = shared.as_ref().unwrap();
            let floor = if one_shard { rq.floor.unwrap_or(Floor::Local) } else { Floor::Local };
            match shard_pass(ix, c, range, floor, &self.pool, &ga, &gb) {
                Ok((a, t, _)) => {
                    touched.add(&t);
                    answers.push(a);
                }
                Err(e) => return error(400, "bad-request", e.to_string()),
            }
            // Every shard compiles the same folder depth and rule; the merge needs them once.
            cut.get_or_insert((c.cut, c.path.clone(), c.sb.is_some()));
        }
        let pass_ms = t_pass.elapsed().as_secs_f64() * 1e3;
        if let Some(d) = rq.deadline_ms {
            if t0.elapsed().as_millis() as u64 > d {
                return error(504, "deadline", format!("{:.0} ms > deadline {d} ms", t0.elapsed().as_secs_f64() * 1e3));
            }
        }
        let (cut, path, diff) = cut.unwrap();
        let echo = json!({"pins": l.pins, "scan": rq.scan, "from": rq.from, "to": rq.to, "path": path, "cell": rq.cell});
        if one_shard {
            let a = answers.pop().unwrap();
            let mut b = json!({"v": 1, "tenant": tenant, "shard": rq.shard, "of": l.shards, "answer": a});
            merge_obj(&mut b, &echo);
            return reply(200, b);
        }
        let t_m = Instant::now();
        let tree = merge(answers, &cut, &path, diff);
        let merge_ms = t_m.elapsed().as_secs_f64() * 1e3;
        let total = if diff {
            json!({"bytes": tree.total[0], "objects": tree.total[1], "bytes_to": tree.total[2], "objects_to": tree.total[3]})
        } else {
            json!({"bytes": tree.total[0], "objects": tree.total[1]})
        };
        let mut b = json!({
            "v": 1, "tenant": tenant, "total": total, "nodes": tree.nodes,
            "timing": {"pass_ms": pass_ms, "merge_ms": merge_ms, "total_ms": t0.elapsed().as_secs_f64() * 1e3, "rows": touched.rows, "dirs": touched.dirs, "shards": l.shards},
        });
        merge_obj(&mut b, &echo);
        if rq.flat {
            b["answer"] = serde_json::to_value(tree.answer("")).unwrap();
        }
        b["tree"] = serde_json::to_value(&tree.root).unwrap();
        reply(200, b)
    }
}

/// `/v1/merge`'s body: shards' partial answers (from `/v1/shard`, any number of machines) and the view they answer.
#[derive(Deserialize)]
pub struct MergeReq {
    pub answers: Vec<ShardAnswer>,
    #[serde(default)]
    pub path: String,
    pub cell: Cell,
    #[serde(default)]
    pub diff: bool,
    #[serde(default)]
    pub flat: bool,
}

/// The merge layer behind HTTP (specs/vm-mode.md §3.2): what a merging caller runs over its shards' answers. Needs no
/// pack: the folder's depth is its path's.
fn merge_req(tenant: &str, body: &[u8]) -> Reply {
    let rq: MergeReq = match serde_json::from_slice(body) {
        Ok(r) => r,
        Err(e) => return error(400, "bad-request", e.to_string()),
    };
    let path = rq.path.trim_end_matches('/').to_string();
    let dp = if path.is_empty() { 0 } else { path.split('/').count() as u8 };
    let cut = match rq.cell.cut(dp) {
        Ok(c) => c,
        Err(e) => return error(400, "bad-request", e.to_string()),
    };
    let tree = merge(rq.answers, &cut, &path, rq.diff);
    let mut b = json!({"v": 1, "tenant": tenant, "path": path, "nodes": tree.nodes, "total": tree.total});
    if rq.flat {
        b["answer"] = serde_json::to_value(tree.answer("")).unwrap();
    }
    b["tree"] = serde_json::to_value(&tree.root).unwrap();
    reply(200, b)
}

fn merge_obj(b: &mut Value, from: &Value) {
    for (k, v) in from.as_object().unwrap() {
        if !v.is_null() {
            b[k] = v.clone();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn m(t: &str) -> Matcher {
        Matcher { kind: "sub".into(), text: t.into(), start: false, end: false }
    }

    #[test]
    fn ast_to_expr() {
        let a = QueryAst { alts: vec![vec![m("Ckpt"), Matcher { start: true, ..m("a/b") }], vec![m("x")]], neg: vec![m("tmp")] };
        let e = expr_of(&a).ok().unwrap().unwrap();
        assert_eq!(
            e,
            Expr::And(vec![
                Expr::Or(vec![Expr::And(vec![Expr::T("ckpt".into()), Expr::P("^a/b".into())]), Expr::T("x".into())]),
                Expr::Not(Box::new(Expr::T("tmp".into()))),
            ])
        );
        let only_neg = QueryAst { alts: vec![vec![]], neg: vec![m("a"), m("b")] };
        assert_eq!(expr_of(&only_neg).ok().unwrap().unwrap(), Expr::Not(Box::new(Expr::Or(vec![Expr::T("a".into()), Expr::T("b".into())]))));
        let re = QueryAst { alts: vec![vec![Matcher { kind: "regex".into(), ..m("x") }]], neg: vec![] };
        assert!(matches!(expr_of(&re), Err(Unsupported(_))));
    }

    #[test]
    fn signatures() {
        let s = request_string("POST", "/v1/view", "gcs", "1700000000", b"{}");
        assert_eq!(s, format!("v1\nPOST\n/v1/view\ngcs\n1700000000\n{}", sha256_hex(b"{}")));
        let sig = sign(b"k", &s);
        assert!(verify(b"k", &s, &sig));
        assert!(!verify(b"k2", &s, &sig));
        assert!(!verify(b"k", &s, "zz"));
    }
}
