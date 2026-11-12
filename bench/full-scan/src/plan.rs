//! The bench's query mix (specs/full-scan-serving-bench.md "Bench"), drawn from a loaded store: the fleet root, each
//! bucket, drilled folders at depths 3 and 6; no / rare / common search, AND / OR / NOT, a regex, path terms; owner and
//! class scopes; staged prefixes; the newest scan, one a month back, and one-day / month diffs.

use std::collections::HashMap;

use crate::dict::Pat;
use crate::index::{Index, FILE};
use crate::query::{Expr, OwnerSel, Query};
use crate::scan::run;
use crate::util::{h128, Result};

const DEPTH: u8 = 3;
const PPM: u64 = 500;

fn q(id: &str, tags: &[&str]) -> Query {
    Query {
        id: id.to_string(),
        scan: None,
        from: None,
        to: None,
        path: String::new(),
        q: None,
        owners: None,
        classes: None,
        staged: None,
        depth: DEPTH,
        min_ppm: PPM,
        tags: tags.iter().map(|s| s.to_string()).collect(),
    }
}

fn t(s: &str) -> Expr {
    Expr::T(s.to_string())
}

/// Dirs at `depth`, as `(rows in subtree, group start)`, largest first.
fn dirs_at(idx: &Index, depth: u8) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let n = idx.n();
    let mut r = 0;
    // Walk group starts, descending into a subtree only while above `depth`.
    while r < n {
        let d = idx.depth[r];
        if d == depth {
            if idx.flags[r] & FILE == 0 {
                out.push((idx.end[r] as usize + 1 - r, r));
            }
            r = idx.end[r] as usize + 1;
        } else if d < depth && idx.flags[r] & FILE == 0 {
            r = idx.group_end(r);
        } else {
            r = idx.end[r] as usize + 1;
        }
    }
    out.sort_by(|a, b| b.cmp(a));
    out
}

pub fn plan(idx: &Index, pool: &rayon::ThreadPool) -> Result<(Vec<Query>, serde_json::Value)> {
    let ns = idx.scans.len();
    let newest = idx.scans[ns - 1].clone();
    let prev = idx.scans[ns.saturating_sub(2)].clone();
    let month = idx.scans.iter().rev().find(|s| s.ts <= newest.ts - 30 * 86400).unwrap_or(&idx.scans[0]).clone();
    let s_new = (ns - 1) as u16;

    let buckets: Vec<String> = dirs_at(idx, 1).iter().map(|&(_, g)| idx.path_of(g)).collect::<Vec<_>>();
    let pick = |v: &[(usize, usize)]| -> Vec<String> {
        let mut out = Vec::new();
        if let Some(&(_, g)) = v.first() {
            out.push(idx.path_of(g));
        }
        if v.len() > 1 {
            out.push(idx.path_of(v[v.len() / 10].1));
        }
        out
    };
    // Folders holding a live object at the newest scan.
    let live = |v: Vec<(usize, usize)>| -> Vec<(usize, usize)> {
        v.into_iter()
            .filter(|&(_, g)| (g..=idx.end[g] as usize).any(|r| idx.flags[r] & FILE != 0 && idx.vf[r] <= s_new && s_new < idx.vt[r]))
            .collect()
    };
    let d3 = pick(&live(dirs_at(idx, 3)));
    let d6 = pick(&live(dirs_at(idx, 6)));
    let d4 = dirs_at(idx, 4);
    let mut staged_rows: Vec<usize> = d4.iter().map(|x| x.1).collect();
    staged_rows.sort_unstable();
    let staged: Vec<String> = staged_rows.iter().step_by((staged_rows.len() / 40).max(1)).take(40).map(|&g| idx.path_of(g)).collect();

    // Owners and classes by bytes at the newest scan (simple files; enough to pick).
    let mut own: HashMap<u16, u64> = HashMap::new();
    let mut cls = [0u64; 4];
    let mut name_files: HashMap<u32, u32> = HashMap::new();
    for r in 0..idx.n() {
        let f = idx.flags[r];
        if f & FILE != 0 && idx.vf[r] <= s_new && s_new < idx.vt[r] {
            *own.entry(idx.owner[r]).or_default() += idx.size[r];
            cls[((f >> 2) & 3) as usize] += idx.size[r];
            *name_files.entry(idx.name[r]).or_default() += 1;
        }
    }
    let mut owners: Vec<(u64, String)> = own.iter().filter(|(&o, _)| o != 0).map(|(&o, &b)| (b, idx.owners[o as usize].clone())).collect();
    owners.sort_by(|a, b| b.cmp(a));
    let cls_pick: Vec<u8> = (1..4u8).filter(|&k| cls[k as usize] > 0).map(|k| k + 1).collect();

    // Rare terms: names of 8+ characters (files' or folders'), sampled by hash, whose search matches 1–1,000 live files.
    let _ = &name_files;
    let mut cands: Vec<(u128, u32)> = (0..idx.dict.len() as u32)
        .filter(|&id| idx.dict.lower(id).len() >= 8)
        .map(|id| (h128(idx.dict.lower(id)), id))
        .collect();
    cands.sort_unstable();
    let mut rare: Vec<String> = Vec::new();
    for (_, id) in cands.iter().take(200) {
        let term = String::from_utf8_lossy(idx.dict.lower(*id)).into_owned();
        if rare.contains(&term) {
            continue;
        }
        let mut probe = q("probe", &[]);
        probe.scan = Some(newest.id.clone());
        probe.depth = 1;
        probe.q = Some(t(&term));
        let (a, _) = run(idx, &probe, pool)?;
        if (1..=1000).contains(&a.total[1]) {
            rare.push(term);
        }
        if rare.len() == 3 {
            break;
        }
    }
    // Common terms present in this store.
    let common: Vec<&str> = ["zarr.json", ".npy", "ckpt", "node_modules"]
        .into_iter()
        .filter(|c| idx.dict.bitset(&Pat::Contains(c.to_string()), 4).count() > 0)
        .collect();

    let mut qs: Vec<Query> = Vec::new();
    let view = |id: &str, path: &str, tags: &[&str]| {
        let mut x = q(id, tags);
        x.scan = Some(newest.id.clone());
        x.path = path.to_string();
        x
    };
    // Folders × time.
    qs.push(view("root", "", &["folder", "root"]));
    for (i, b) in buckets.iter().enumerate() {
        qs.push(view(&format!("bucket{i}"), b, &["folder", "bucket"]));
    }
    for (i, p) in d3.iter().enumerate() {
        qs.push(view(&format!("d3-{i}"), p, &["folder", "d3"]));
    }
    for (i, p) in d6.iter().enumerate() {
        qs.push(view(&format!("d6-{i}"), p, &["folder", "d6"]));
    }
    let mut deep = view("root-depth6", "", &["folder", "root", "deep"]);
    deep.depth = 6;
    qs.push(deep);
    for (id, path) in [("root-month", ""), ("d3-0-month", d3.first().map(|s| s.as_str()).unwrap_or(""))] {
        let mut x = view(id, path, &["folder", "old"]);
        x.scan = Some(month.id.clone());
        qs.push(x);
    }
    let diff = |id: &str, path: &str, from: &str, tags: &[&str]| {
        let mut x = q(id, tags);
        x.from = Some(from.to_string());
        x.to = Some(newest.id.clone());
        x.path = path.to_string();
        x
    };
    let b0 = buckets.first().cloned().unwrap_or_default();
    let d30 = d3.first().cloned().unwrap_or_default();
    for (sfx, from) in [("day", prev.id.as_str()), ("month", month.id.as_str())] {
        qs.push(diff(&format!("diff-{sfx}-root"), "", from, &["diff", sfx]));
        qs.push(diff(&format!("diff-{sfx}-bucket0"), &b0, from, &["diff", sfx]));
        qs.push(diff(&format!("diff-{sfx}-d3"), &d30, from, &["diff", sfx]));
    }
    // Search.
    let search = |id: &str, path: &str, e: Expr, tags: &[&str]| {
        let mut x = view(id, path, tags);
        x.q = Some(e);
        x
    };
    for (i, r) in rare.iter().enumerate() {
        qs.push(search(&format!("rare{i}"), "", t(r), &["search", "rare"]));
    }
    if let Some(r) = rare.first() {
        qs.push(search("rare0-bucket0", &b0, t(r), &["search", "rare"]));
    }
    for c in &common {
        qs.push(search(&format!("common-{c}"), "", t(c), &["search", "common"]));
        qs.push(search(&format!("common-{c}-d3"), &d30, t(c), &["search", "common"]));
    }
    let c0 = common.first().copied().unwrap_or("zarr.json");
    let c1 = common.get(1).copied().unwrap_or(".npy");
    qs.push(search("and", "", Expr::And(vec![t("checkpoints"), t(c0)]), &["search", "bool"]));
    qs.push(search("or", "", Expr::Or(vec![t(c0), t(c1)]), &["search", "bool"]));
    qs.push(search("not", "", Expr::Not(Box::new(t(c1))), &["search", "bool"]));
    qs.push(search("and-not", "", Expr::And(vec![t("checkpoints"), Expr::Not(Box::new(t(c0)))]), &["search", "bool"]));
    qs.push(search("anchor-start", "", t("^step-"), &["search", "anchor"]));
    qs.push(search("anchor-end", "", t(".json$"), &["search", "anchor"]));
    qs.push(search("anchor-exact", "", t("^zarr.json$"), &["search", "anchor"]));
    qs.push(search("regex", "", Expr::Re(r"^step-0+[0-9]*5000$".to_string()), &["search", "regex"]));
    qs.push(search("regex-bucket0", &b0, Expr::Re(r"^shard_0+[1-3]".to_string()), &["search", "regex"]));
    qs.push(search("path-term", "", Expr::P("c/0/0".to_string()), &["search", "path"]));
    qs.push(search("path-term-anchored", "", Expr::P("^checkpoints/run-".to_string()), &["search", "path"]));
    let mut x = search("search-diff-month", "", t(c0), &["search", "diff"]);
    x.scan = None;
    x.from = Some(month.id.clone());
    x.to = Some(newest.id.clone());
    qs.push(x);
    let mut x = search("search-month", "", t(c1), &["search", "old"]);
    x.scan = Some(month.id.clone());
    qs.push(x);
    // Owners.
    let scoped = |id: &str, path: &str, sel: OwnerSel, tags: &[&str]| {
        let mut x = view(id, path, tags);
        x.owners = Some(sel);
        x
    };
    if let Some((_, o)) = owners.first() {
        qs.push(scoped("owner-one", "", OwnerSel::In(vec![o.clone()]), &["owner"]));
        qs.push(scoped("owner-one-bucket0", &b0, OwnerSel::In(vec![o.clone()]), &["owner"]));
        let mut x = scoped("owner-one-diff-month", "", OwnerSel::In(vec![o.clone()]), &["owner", "diff"]);
        x.scan = None;
        x.from = Some(month.id.clone());
        x.to = Some(newest.id.clone());
        qs.push(x);
    }
    qs.push(scoped("owned", "", OwnerSel::NotIn(vec![String::new()]), &["owner"]));
    qs.push(scoped("unowned", "", OwnerSel::In(vec![String::new()]), &["owner"]));
    qs.push(scoped("unowned-d3", &d30, OwnerSel::In(vec![String::new()]), &["owner"]));
    let two: Vec<String> = owners.iter().take(2).map(|o| o.1.clone()).collect();
    qs.push(scoped("all-but-two", "", OwnerSel::NotIn(two.clone()), &["owner"]));
    let mut x = scoped("owned-search", "", OwnerSel::NotIn(vec![String::new()]), &["owner", "search"]);
    x.q = Some(t(c1));
    qs.push(x);
    // Classes.
    if let Some(&k) = cls_pick.first() {
        let mut x = view("class-one", "", &["class"]);
        x.classes = Some(vec![k]);
        qs.push(x);
        let mut x = view("class-one-bucket0", &b0, &["class"]);
        x.classes = Some(vec![k]);
        qs.push(x);
        let mut x = view("class-one-search", "", &["class", "search"]);
        x.classes = Some(vec![k]);
        x.q = Some(t(c0));
        qs.push(x);
        if let Some((_, o)) = owners.first() {
            let mut x = view("class-owner", "", &["class", "owner"]);
            x.classes = Some(vec![k]);
            x.owners = Some(OwnerSel::In(vec![o.clone()]));
            qs.push(x);
        }
    }
    let mut x = view("class-standard", "", &["class"]);
    x.classes = Some(vec![1]);
    qs.push(x);
    // Staged.
    let mut x = view("staged", "", &["staged"]);
    x.staged = Some(staged.clone());
    qs.push(x);
    let mut x = view("staged-owned", "", &["staged", "owner"]);
    x.staged = Some(staged.clone());
    x.owners = Some(OwnerSel::NotIn(vec![String::new()]));
    qs.push(x);
    let mut x = diff("staged-diff-month", "", &month.id, &["staged", "diff"]);
    x.staged = Some(staged.clone());
    qs.push(x);
    let mut x = view("staged-search", "", &["staged", "search"]);
    x.staged = Some(staged.clone());
    x.q = Some(t(c0));
    qs.push(x);
    // Drilled searches and depth-6 drills over time.
    for (i, p) in d6.iter().enumerate() {
        qs.push(search(&format!("d6-{i}-search"), p, t(c0), &["search", "d6"]));
        qs.push(diff(&format!("diff-month-d6-{i}"), p, &month.id, &["diff", "d6"]));
    }
    for (i, b) in buckets.iter().enumerate().skip(1) {
        qs.push(diff(&format!("diff-day-bucket{i}"), b, &prev.id, &["diff", "bucket"]));
    }
    let meta = serde_json::json!({
        "newest": newest.id, "prev": prev.id, "month": month.id, "buckets": buckets, "d3": d3, "d6": d6,
        "rare": rare, "common": common, "owners": owners.iter().take(4).map(|o| &o.1).collect::<Vec<_>>(),
        "classes": cls_pick, "staged": staged.len(),
    });
    Ok((qs, meta))
}

/// An exhaustive mix for small stores (the fixtures): every scan and consecutive diff, every folder (up to 24), each
/// filter alone and in combinations, at several depths and thresholds.
pub fn exhaustive(idx: &Index) -> Vec<Query> {
    let mut folders = vec![String::new()];
    let mut r = 0;
    while r < idx.n() && folders.len() < 24 {
        if idx.flags[r] & FILE == 0 && idx.flags[r] & crate::index::CONT == 0 {
            folders.push(idx.path_of(r));
        }
        r += 1;
    }
    let names: Vec<String> = (0..idx.dict.len() as u32).map(|i| String::from_utf8_lossy(idx.dict.lower(i)).into_owned()).collect();
    let mut terms: Vec<Expr> = Vec::new();
    for (i, n) in names.iter().enumerate().step_by((names.len() / 4).max(1)).take(4) {
        let short: String = n.chars().take(2).collect();
        terms.push(t(&short));
        terms.push(t(&format!("^{n}")));
        terms.push(t(&format!("{}$", n.chars().skip(1).collect::<String>())));
        terms.push(Expr::Re(format!("^{}", regex::escape(&short))));
        if i % 2 == 0 {
            terms.push(t(&format!("^{n}$")));
        }
    }
    // Path terms from real two- and three-segment spans.
    for f in folders.iter().filter(|f| f.matches('/').count() >= 1).take(3) {
        let segs: Vec<&str> = f.split('/').collect();
        let k = segs.len();
        terms.push(Expr::P(format!("{}/{}", &segs[k - 2][segs[k - 2].len().saturating_sub(1)..], &segs[k - 1][..1.min(segs[k - 1].len())])));
        terms.push(Expr::P(format!("^{}/{}$", segs[k - 2], segs[k - 1])));
    }
    if terms.len() >= 2 {
        let (a, b) = (terms[0].clone(), terms[1].clone());
        terms.push(Expr::And(vec![a.clone(), b.clone()]));
        terms.push(Expr::Or(vec![a.clone(), b.clone()]));
        terms.push(Expr::Not(Box::new(a.clone())));
        terms.push(Expr::And(vec![Expr::Or(vec![a.clone(), terms[2].clone()]), Expr::Not(Box::new(b))]));
    }
    let owners: Vec<OwnerSel> = {
        let mut v: Vec<OwnerSel> = idx.owners.iter().map(|o| OwnerSel::In(vec![o.clone()])).collect();
        v.push(OwnerSel::NotIn(vec![String::new()]));
        if idx.owners.len() > 1 {
            v.push(OwnerSel::NotIn(vec![idx.owners[1].clone()]));
        }
        v
    };
    let staged: Vec<String> = folders.iter().skip(1).step_by(3).take(3).cloned().collect();
    let mut times: Vec<(Option<String>, Option<String>, Option<String>)> = idx.scans.iter().map(|s| (Some(s.id.clone()), None, None)).collect();
    for w in idx.scans.windows(2) {
        times.push((None, Some(w[0].id.clone()), Some(w[1].id.clone())));
    }
    if idx.scans.len() > 2 {
        times.push((None, Some(idx.scans[0].id.clone()), Some(idx.scans[idx.scans.len() - 1].id.clone())));
    }
    let mut qs = Vec::new();
    let mut i = 0usize;
    for (ti, (scan, from, to)) in times.iter().enumerate() {
        for (fi, folder) in folders.iter().enumerate() {
            // Filters rotate across (time, folder) cells so every one meets every time and folder shape.
            let mut variants: Vec<Query> = Vec::new();
            let mut base = q("", &[]);
            base.scan = scan.clone();
            base.from = from.clone();
            base.to = to.clone();
            base.path = folder.clone();
            variants.push(base.clone());
            let k = ti * 7 + fi;
            if !terms.is_empty() {
                let mut x = base.clone();
                x.q = Some(terms[k % terms.len()].clone());
                variants.push(x);
            }
            let mut x = base.clone();
            x.owners = Some(owners[k % owners.len()].clone());
            variants.push(x);
            let mut x = base.clone();
            x.classes = Some(vec![1 + (k % 4) as u8, 1 + ((k + 2) % 4) as u8]);
            variants.push(x);
            let mut x = base.clone();
            x.staged = Some(staged.clone());
            if !terms.is_empty() {
                x.q = Some(terms[(k + 3) % terms.len()].clone());
            }
            variants.push(x);
            for mut v in variants {
                v.depth = 1 + (i % 3) as u8;
                v.min_ppm = [0, 0, 1000, 200_000][i % 4];
                v.id = format!("x{i:04}");
                i += 1;
                qs.push(v);
            }
        }
    }
    qs
}
