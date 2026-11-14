//! `fsb`: load an interval-store generation into RAM and answer views by one pass (specs/full-scan-serving-bench.md).
//!
//! ```text
//! SRC below is a generation dir (loaded from the store), or a pack file (`fsb pack` / `fsb slim`), mapped.
//!
//! fsb load GEN                                         load, print stats and bytes per column (JSON)
//! fsb pack GEN -o FILE                                 load, write the full index as a pack file
//! fsb pack-slim GEN -o FILE                            load, write the slim index as a pack file (no full pack)
//! fsb shard PACK -n N -o DIR                           a slim pack cut into N physical shard packs
//! fsb slim SRC -o FILE                                 the slim index (object versions + directory structure) of
//!                                                      SRC's full index, written as a pack file
//! fsb plan GEN -o QUERIES [-t N] [-x 1 | -c 1]         the bench's query mix for this store (`-x`: the fixtures'
//!                                                      exhaustive mix; `-c`: that plus cell-rule twins)
//! fsb run GEN QUERIES -o ANSWERS [-t N]                answer each query (JSONL)
//! fsb bench SRC QUERIES -o RESULTS -a ANSWERS [-t 1,16] [-r 5] [-l LABEL] [-A REF] [-p N]
//!                                                      cold + warm runs per thread count, answers checked equal
//!                                                      (QUERIES planned first when it doesn't exist); `-A`: each
//!                                                      answer equal to REF's (exit 1 after the run if not); `-p N`:
//!                                                      fault a pack file in on N threads before the first query;
//!                                                      `-m N`: read it into huge-page memory on N threads instead.
//!                                                      Each run's storage reads and major faults are recorded.
//! fsb synth -o STAGE [-S 30] [-R 2] [-x SCALE] [-s SEED]  a synthetic fleet's history (`oracle/synth_store.py` cuts it)
//! ```

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::time::Instant;

use full_scan::load::load;
use full_scan::pack::{self, Pack};
use full_scan::scan::Timing;
use full_scan::slim::SlimIndex;
use full_scan::slim_scan::{self, Touched};
use full_scan::plan::plan;
use full_scan::query::Query;
use full_scan::scan::{run, Answer};
use full_scan::store;
use full_scan::synth::Synth;
use full_scan::util::{commas, err, Result};
use serde_json::json;

struct Args {
    pos: Vec<String>,
    opts: HashMap<String, String>,
}

fn args() -> Args {
    let mut pos = Vec::new();
    let mut opts = HashMap::new();
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        if a.starts_with('-') && a.len() == 2 {
            opts.insert(a[1..].to_string(), it.next().unwrap_or_default());
        } else {
            pos.push(a);
        }
    }
    Args { pos, opts }
}

fn pool(n: usize) -> Result<rayon::ThreadPool> {
    Ok(rayon::ThreadPoolBuilder::new().num_threads(n).build()?)
}

fn read_queries(p: &str) -> Result<Vec<Query>> {
    let r = BufReader::new(File::open(p)?);
    let mut out = Vec::new();
    for l in r.lines() {
        let l = l?;
        if !l.trim().is_empty() {
            out.push(serde_json::from_str(&l)?);
        }
    }
    Ok(out)
}

fn load_gen(dir: &str) -> Result<full_scan::index::Index> {
    let g = store::open(&PathBuf::from(dir))?;
    eprintln!("load: {} scans, {} files (manifest {:?})", g.scans.len(), g.files.len(), g.manifest);
    let idx = load(&g)?;
    log_full(&idx);
    Ok(idx)
}

fn log_full(idx: &full_scan::index::Index) {
    let cb = idx.column_bytes();
    eprintln!(
        "index: {} rows ({} file versions, {} dirs), {} names, {} owners, {} resident ({:.1} B/row)",
        commas(idx.stats.rows),
        commas(idx.stats.file_rows),
        commas(idx.stats.dir_rows),
        commas(idx.stats.names),
        idx.stats.owners,
        commas(cb.total),
        cb.per_row,
    );
}

enum Engine {
    Full(full_scan::index::Index),
    Slim(SlimIndex),
}

impl Engine {
    /// None: a query this index doesn't answer (directory totals on the slim index).
    fn run(&self, q: &Query, p: &rayon::ThreadPool) -> Result<Option<(Answer, Timing, Option<Touched>)>> {
        Ok(match self {
            Engine::Full(idx) => Some(run(idx, q, p).map(|(a, t)| (a, t, None))?),
            Engine::Slim(_) if q.dirs => None,
            Engine::Slim(ix) => Some(slim_scan::run(ix, q, p).map(|(a, t, x)| (a, t, Some(x)))?),
        })
    }

    fn info(&self) -> serde_json::Value {
        match self {
            Engine::Full(idx) => json!({"index": "full", "stats": idx.stats, "columns": idx.column_bytes()}),
            Engine::Slim(ix) => json!({"index": "slim", "stats": ix.stats, "columns": ix.column_bytes()}),
        }
    }
}

/// SRC: a generation dir (loaded), or a pack file: mapped (`prefault` threads fault it all in first), or with `mem`
/// threads read into huge-page-backed memory.
fn open_src(src: &str, prefault: Option<usize>, mem: Option<usize>) -> Result<(Engine, serde_json::Value)> {
    let path = PathBuf::from(src);
    let p0 = pack::proc();
    let t0 = Instant::now();
    if path.is_dir() {
        let idx = load_gen(src)?;
        let s = t0.elapsed().as_secs_f64();
        return Ok((Engine::Full(idx), json!({"src": "store", "load_s": s, "proc": pack::proc()})));
    }
    let p = match mem {
        Some(n) => Pack::read(&path, n)?,
        None => Pack::open(&path)?,
    };
    let open_s = t0.elapsed().as_secs_f64();
    let fault_s = match prefault {
        Some(n) => Some(p.prefault(n)?),
        None => None,
    };
    let e = match p.kind.as_str() {
        "full" => Engine::Full(pack::read_index(&p)?),
        "slim" => Engine::Slim(SlimIndex::read(&p)?),
        k => return err(format!("pack kind {k}")),
    };
    let load_s = t0.elapsed().as_secs_f64();
    let p1 = pack::proc();
    let mb = p.len() as f64 / 1048576.0;
    let info = json!({
        "src": "pack", "kind": p.kind, "file_bytes": p.len(), "open_s": open_s, "prefault_s": fault_s,
        "prefault_mib_s": fault_s.map(|f| mb / f), "mem": mem.is_some(), "mem_mib_s": mem.map(|_| mb / open_s), "load_s": load_s, "read_bytes": p1.read_bytes - p0.read_bytes,
        "majflt": p1.majflt - p0.majflt, "proc": p1,
    });
    eprintln!("open {src}: {}", info);
    Ok((e, info))
}

fn median(v: &mut [f64]) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    if v.is_empty() {
        0.0
    } else {
        v[v.len() / 2]
    }
}

fn main() -> Result<()> {
    let a = args();
    let cmd = a.pos.first().map(|s| s.as_str()).unwrap_or("");
    let threads = |d: usize| a.opts.get("t").map(|t| t.parse().unwrap()).unwrap_or(d);
    match cmd {
        "load" => {
            let idx = load_gen(&a.pos[1])?;
            println!("{}", serde_json::to_string_pretty(&json!({"stats": idx.stats, "columns": idx.column_bytes()}))?);
        }
        "pack" => {
            let idx = load_gen(&a.pos[1])?;
            let t = Instant::now();
            let n = pack::write_index(&idx, &PathBuf::from(a.opts.get("o").ok_or("-o FILE")?))?;
            println!("{}", json!({"bytes": n, "write_s": t.elapsed().as_secs_f64(), "load": idx.stats, "proc": pack::proc()}));
        }
        "pack-slim" => {
            // The VM-mode pack writer (specs/vm-mode.md §6.1): the store straight to the slim pack, no full pack. The
            // pack records its generation and manifest (`stats.pins`), which `vmd` checks requests against.
            let t0 = Instant::now();
            let dir = PathBuf::from(&a.pos[1]);
            let g = store::open(&dir)?;
            let gen = dir.canonicalize()?.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let pins = json!({"gen": gen, "manifest": g.manifest.clone().unwrap_or_default()});
            let idx = load_gen(&a.pos[1])?;
            let load_s = t0.elapsed().as_secs_f64();
            let t = Instant::now();
            let mut ix = SlimIndex::from_index(&idx)?;
            ix.stats["pins"] = pins;
            let build_s = t.elapsed().as_secs_f64();
            let load = serde_json::to_value(&idx.stats)?;
            drop(idx);
            let t = Instant::now();
            let n = ix.write(&PathBuf::from(a.opts.get("o").ok_or("-o FILE")?))?;
            println!("{}", json!({"bytes": n, "load_s": load_s, "build_s": build_s, "write_s": t.elapsed().as_secs_f64(), "load": load, "stats": ix.stats, "scans": ix.scans.len(), "proc": pack::proc()}));
        }
        "shard" => {
            // Physical shards (specs/vm-mode.md §3.1): a slim pack cut into `-n` row ranges, each a slim pack of its own.
            let p = Pack::open(&PathBuf::from(&a.pos[1]))?;
            let ix = SlimIndex::read(&p)?;
            let k: usize = a.opts.get("n").ok_or("-n N")?.parse()?;
            let out = PathBuf::from(a.opts.get("o").ok_or("-o DIR")?);
            std::fs::create_dir_all(&out)?;
            let n = ix.n_o();
            for i in 0..k {
                let mut s = ix.slice(n * i / k, n * (i + 1) / k, i as u64, k as u64)?;
                if let Some(pins) = ix.stats.get("pins") {
                    s.stats["pins"] = pins.clone();
                }
                let f = out.join(format!("shard-{i}-of-{k}.fsb"));
                let b = s.write(&f)?;
                println!("{}", json!({"file": f, "bytes": b, "stats": s.stats}));
            }
        }
        "slim" => {
            let (e, _) = open_src(&a.pos[1], None, None)?;
            let Engine::Full(idx) = e else { return err("slim: SRC must be a store or a full pack") };
            let t = Instant::now();
            let ix = SlimIndex::from_index(&idx)?;
            let build_s = t.elapsed().as_secs_f64();
            drop(idx);
            let t = Instant::now();
            let n = ix.write(&PathBuf::from(a.opts.get("o").ok_or("-o FILE")?))?;
            println!("{}", json!({"bytes": n, "build_s": build_s, "write_s": t.elapsed().as_secs_f64(), "stats": ix.stats, "columns": ix.column_bytes(), "proc": pack::proc()}));
        }
        "plan" => {
            let (e, _) = open_src(&a.pos[1], None, None)?;
            let Engine::Full(idx) = e else { return err("plan: needs the full index") };
            let p = pool(threads(num_cpus()))?;
            let (qs, meta) = if a.opts.contains_key("c") {
                (full_scan::plan::exhaustive_cells(&idx), json!({}))
            } else if a.opts.contains_key("x") {
                (full_scan::plan::exhaustive(&idx), json!({}))
            } else {
                plan(&idx, &p)?
            };
            let mut f = File::create(a.opts.get("o").ok_or("-o QUERIES")?)?;
            for q in &qs {
                writeln!(f, "{}", serde_json::to_string(q)?)?;
            }
            eprintln!("plan: {} queries; {}", qs.len(), meta);
        }
        "run" => {
            let (e, _) = open_src(&a.pos[1], None, None)?;
            let qs = read_queries(&a.pos[2])?;
            let p = pool(threads(num_cpus()))?;
            let mut f = File::create(a.opts.get("o").ok_or("-o ANSWERS")?)?;
            for q in &qs {
                let Some((ans, t, _)) = e.run(q, &p)? else { continue };
                eprintln!("{}: {:.1} ms ({} rows, {} nodes)", q.id, t.total_ms, commas(t.rows), ans.nodes.len());
                writeln!(f, "{}", serde_json::to_string(&ans)?)?;
            }
        }
        "bench" => {
            let prefault = a.opts.get("p").map(|x| x.parse().unwrap());
            let mem = a.opts.get("m").map(|x| x.parse().unwrap());
            let (e, open) = open_src(&a.pos[1], prefault, mem)?;
            // No query file yet: plan one from this load (one load per store, not two).
            if !std::path::Path::new(&a.pos[2]).exists() {
                let Engine::Full(idx) = &e else { return err("bench: planning needs the full index") };
                let (qs, meta) = plan(idx, &pool(num_cpus())?)?;
                let mut f = File::create(&a.pos[2])?;
                for q in &qs {
                    writeln!(f, "{}", serde_json::to_string(q)?)?;
                }
                eprintln!("plan: {} queries; {}", qs.len(), meta);
            }
            let qs = read_queries(&a.pos[2])?;
            let reference: Option<HashMap<String, Answer>> = match a.opts.get("A") {
                None => None,
                Some(p) => {
                    let mut m = HashMap::new();
                    for l in BufReader::new(File::open(p)?).lines() {
                        let l = l?;
                        if !l.trim().is_empty() {
                            let ans: Answer = serde_json::from_str(&l)?;
                            m.insert(ans.id.clone(), ans);
                        }
                    }
                    Some(m)
                }
            };
            let label = a.opts.get("l").cloned().unwrap_or_default();
            let reps: usize = a.opts.get("r").map(|r| r.parse().unwrap()).unwrap_or(5);
            let ts: Vec<usize> = a.opts.get("t").map(|t| t.split(',').map(|x| x.parse().unwrap()).collect()).unwrap_or(vec![1, num_cpus()]);
            let mut out = File::create(a.opts.get("o").ok_or("-o RESULTS")?)?;
            writeln!(out, "{}", json!({"kind": "load", "label": label, "open": open, "info": e.info(), "queries": qs.len()}))?;
            let mut answers: HashMap<String, Answer> = HashMap::new();
            let mut differ: Vec<String> = Vec::new();
            let mut first = true;
            for &t in &ts {
                let p = pool(t)?;
                // Cold: every query once, in order, after the load (or the previous thread count's runs).
                let mut cold = HashMap::new();
                for q in &qs {
                    let p0 = pack::proc();
                    let Some((ans, tm, touched)) = e.run(q, &p)? else { continue };
                    let p1 = pack::proc();
                    if first {
                        first = false;
                        writeln!(out, "{}", json!({"kind": "first", "label": label, "id": q.id, "ms": tm.total_ms, "proc": p1}))?;
                        eprintln!("first answer ({}) at {:.2} s since process start", q.id, p1.since_start_s);
                    }
                    if let Some(prev) = answers.get(&q.id) {
                        if prev != &ans {
                            return err(format!("{}: the answer at {t} threads differs from the first", q.id));
                        }
                    } else {
                        if let Some(r) = &reference {
                            if r.get(&q.id) != Some(&ans) {
                                eprintln!("{}: differs from the reference answer", q.id);
                                differ.push(q.id.clone());
                            }
                        }
                        answers.insert(q.id.clone(), ans);
                    }
                    cold.insert(q.id.clone(), (tm, touched, p1.read_bytes - p0.read_bytes, p1.majflt - p0.majflt));
                }
                for q in &qs {
                    let Some((c, touched, c_read, c_flt)) = cold.get(&q.id) else { continue };
                    let mut total = Vec::new();
                    let mut pass = Vec::new();
                    let mut compile = Vec::new();
                    let mut last = None;
                    let p0 = pack::proc();
                    for _ in 0..reps {
                        let (ans, tm, _) = e.run(q, &p)?.unwrap();
                        if &ans != answers.get(&q.id).unwrap() {
                            return err(format!("{}: a warm answer differs from the first", q.id));
                        }
                        total.push(tm.total_ms);
                        pass.push(tm.pass_ms);
                        compile.push(tm.compile_ms);
                        last = Some(tm);
                    }
                    let p1 = pack::proc();
                    let tm = last.unwrap();
                    let ans = &answers[&q.id];
                    let warm = median(&mut total.clone());
                    let pass_med = median(&mut pass);
                    let line = json!({
                        "kind": "query", "label": label, "id": q.id, "tags": q.tags, "threads": t,
                        "cold_ms": c.total_ms, "cold_pass_ms": c.pass_ms, "cold_read_bytes": c_read, "cold_majflt": c_flt,
                        "warm_ms": total, "warm_median_ms": warm, "warm_read_bytes": p1.read_bytes - p0.read_bytes,
                        "warm_majflt": p1.majflt - p0.majflt,
                        "warm_pass_median_ms": pass_med, "warm_compile_median_ms": median(&mut compile),
                        "rows": tm.rows, "bytes": tm.bytes, "chunks": tm.chunks, "touched": touched,
                        "rows_per_s": tm.rows as f64 / (pass_med / 1e3).max(1e-9),
                        "nodes": ans.nodes.len(), "rest": ans.rest.len(), "total": ans.total, "rss_gb": p1.rss_gb,
                    });
                    writeln!(out, "{line}")?;
                    eprintln!("{label} t={t} {}: cold {:.1} ms, warm {:.1} ms (pass {:.1}), {} rows, {} nodes", q.id, c.total_ms, warm, pass_med, commas(tm.rows), ans.nodes.len());
                }
            }
            let mut f = File::create(a.opts.get("a").ok_or("-a ANSWERS")?)?;
            for q in &qs {
                if let Some(ans) = answers.get(&q.id) {
                    writeln!(f, "{}", serde_json::to_string(ans)?)?;
                }
            }
            if let Some(r) = &reference {
                let checked = answers.keys().filter(|k| r.contains_key(*k)).count();
                eprintln!("reference: {} answers checked, {} differ {:?}", checked, differ.len(), differ);
                writeln!(out, "{}", json!({"kind": "reference", "checked": checked, "answered": answers.len(), "differ": differ}))?;
                if !differ.is_empty() {
                    return err(format!("{} answers differ from the reference", differ.len()));
                }
            }
        }
        "synth" => {
            let stage = PathBuf::from(a.opts.get("o").ok_or("-o STAGE")?);
            let scans = a.opts.get("S").map(|x| x.parse().unwrap()).unwrap_or(30);
            let runs = a.opts.get("R").map(|x| x.parse().unwrap()).unwrap_or(2);
            let scale: f64 = a.opts.get("x").map(|x| x.parse().unwrap()).unwrap_or(1.0);
            let seed = a.opts.get("s").map(|x| x.parse().unwrap()).unwrap_or(1);
            let t0 = Instant::now();
            let s = Synth::run(&stage, scans, runs, scale, seed)?;
            eprintln!("synth: {} versions ({} of objects) in {:.1}s", commas(s.versions), commas(s.file_versions), t0.elapsed().as_secs_f64());
        }
        _ => return err("usage: fsb load|plan|run|bench|synth … (see src/main.rs)"),
    }
    Ok(())
}

fn num_cpus() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}
