//! `fsb`: load an interval-store generation into RAM and answer views by one pass (specs/full-scan-serving-bench.md).
//!
//! ```text
//! fsb load GEN                                         load, print stats and bytes per column (JSON)
//! fsb plan GEN -o QUERIES [-t N]                       the bench's query mix for this store
//! fsb run GEN QUERIES -o ANSWERS [-t N]                answer each query (JSONL)
//! fsb bench GEN QUERIES -o RESULTS -a ANSWERS [-t 1,16] [-r 5] [-l LABEL]
//!                                                      cold + warm runs per thread count, answers checked equal
//! fsb synth -o STAGE [-S 30] [-R 2] [-x SCALE] [-s SEED]  a synthetic fleet's history (`oracle/synth_store.py` cuts it)
//! ```

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::time::Instant;

use full_scan::load::load;
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
    let cb = idx.column_bytes();
    eprintln!(
        "load: {} rows ({} file versions, {} dirs), {} names, {} owners, {} resident ({:.1} B/row), {:.1}s",
        commas(idx.stats.rows),
        commas(idx.stats.file_rows),
        commas(idx.stats.dir_rows),
        commas(idx.stats.names),
        idx.stats.owners,
        commas(cb.total),
        cb.per_row,
        idx.stats.secs.last().map_or(0.0, |s| s.1)
    );
    Ok(idx)
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
        "plan" => {
            let idx = load_gen(&a.pos[1])?;
            let p = pool(threads(num_cpus()))?;
            let (qs, meta) = if a.opts.contains_key("x") { (full_scan::plan::exhaustive(&idx), json!({})) } else { plan(&idx, &p)? };
            let mut f = File::create(a.opts.get("o").ok_or("-o QUERIES")?)?;
            for q in &qs {
                writeln!(f, "{}", serde_json::to_string(q)?)?;
            }
            eprintln!("plan: {} queries; {}", qs.len(), meta);
        }
        "run" => {
            let idx = load_gen(&a.pos[1])?;
            let qs = read_queries(&a.pos[2])?;
            let p = pool(threads(num_cpus()))?;
            let mut f = File::create(a.opts.get("o").ok_or("-o ANSWERS")?)?;
            for q in &qs {
                let (ans, t) = run(&idx, q, &p)?;
                eprintln!("{}: {:.1} ms ({} rows, {} nodes)", q.id, t.total_ms, commas(t.rows), ans.nodes.len());
                writeln!(f, "{}", serde_json::to_string(&ans)?)?;
            }
        }
        "bench" => {
            let t0 = Instant::now();
            let idx = load_gen(&a.pos[1])?;
            let load_s = t0.elapsed().as_secs_f64();
            let qs = read_queries(&a.pos[2])?;
            let label = a.opts.get("l").cloned().unwrap_or_default();
            let reps: usize = a.opts.get("r").map(|r| r.parse().unwrap()).unwrap_or(5);
            let ts: Vec<usize> = a.opts.get("t").map(|t| t.split(',').map(|x| x.parse().unwrap()).collect()).unwrap_or(vec![1, num_cpus()]);
            let mut out = File::create(a.opts.get("o").ok_or("-o RESULTS")?)?;
            writeln!(out, "{}", json!({"kind": "load", "label": label, "load_s": load_s, "stats": idx.stats, "columns": idx.column_bytes(), "queries": qs.len()}))?;
            let mut answers: HashMap<String, Answer> = HashMap::new();
            for &t in &ts {
                let p = pool(t)?;
                // Cold: every query once, in order, after the load (or the previous thread count's runs).
                let mut cold = HashMap::new();
                for q in &qs {
                    let (ans, tm) = run(&idx, q, &p)?;
                    if let Some(prev) = answers.get(&q.id) {
                        if prev != &ans {
                            return err(format!("{}: the answer at {t} threads differs from the first", q.id));
                        }
                    } else {
                        answers.insert(q.id.clone(), ans);
                    }
                    cold.insert(q.id.clone(), tm);
                }
                for q in &qs {
                    let mut total = Vec::new();
                    let mut pass = Vec::new();
                    let mut compile = Vec::new();
                    let mut last = None;
                    for _ in 0..reps {
                        let (ans, tm) = run(&idx, q, &p)?;
                        if &ans != answers.get(&q.id).unwrap() {
                            return err(format!("{}: a warm answer differs from the first", q.id));
                        }
                        total.push(tm.total_ms);
                        pass.push(tm.pass_ms);
                        compile.push(tm.compile_ms);
                        last = Some(tm);
                    }
                    let tm = last.unwrap();
                    let c = &cold[&q.id];
                    let ans = &answers[&q.id];
                    let warm = median(&mut total.clone());
                    let pass_med = median(&mut pass);
                    let line = json!({
                        "kind": "query", "label": label, "id": q.id, "tags": q.tags, "threads": t,
                        "cold_ms": c.total_ms, "cold_pass_ms": c.pass_ms, "warm_ms": total, "warm_median_ms": warm,
                        "warm_pass_median_ms": pass_med, "warm_compile_median_ms": median(&mut compile),
                        "rows": tm.rows, "bytes": tm.bytes, "chunks": tm.chunks,
                        "rows_per_s": tm.rows as f64 / (pass_med / 1e3).max(1e-9),
                        "nodes": ans.nodes.len(), "rest": ans.rest.len(), "total": ans.total,
                    });
                    writeln!(out, "{line}")?;
                    eprintln!("{label} t={t} {}: cold {:.1} ms, warm {:.1} ms (pass {:.1}), {} rows, {} nodes", q.id, c.total_ms, warm, pass_med, commas(tm.rows), ans.nodes.len());
                }
            }
            let mut f = File::create(a.opts.get("a").ok_or("-a ANSWERS")?)?;
            for q in &qs {
                writeln!(f, "{}", serde_json::to_string(&answers[&q.id])?)?;
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
