//! Load a generation's `path` sorts (base + runs) into the packed index.
//!
//! The served sorts are ordered `(depth, path, vf)` per segment, not depth first, so the load goes one depth at a time:
//! level `d`'s rows are read from the row groups whose `depth` statistics hold `d`, each row's parent found by its
//! path's prefix in level `d − 1`'s map (128-bit path hashes, one level held at a time), its versions combined as the
//! reader's `combineLive` does (one row per `(depth, path, vf)`, the smallest `vt`), directory versions collapsed into
//! one structural row (only objects carry bytes into a view). Then each level's nodes are ordered by
//! `(parent's position, name + "/")`, which is depth-first order, and every version is scattered to its row.

use std::collections::HashMap;
use std::fs::File;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use arrow_array::RecordBatch;
use parquet::arrow::arrow_reader::{ArrowReaderMetadata, ArrowReaderOptions, ParquetRecordBatchReaderBuilder};
use parquet::arrow::ProjectionMask;
use parquet::file::statistics::Statistics;
use rayon::prelude::*;

use crate::dict::{cmp_slash, Dict};
use crate::index::{Complex, Index, LoadStats, CK, CLS_SHIFT, COMPLEX, CONT, FILE};
use crate::store::{Generation, OPEN};
use crate::util::{err, h128, ints, IdMap, Result, Shared, Strs};

const COLS: [&str; 11] = ["depth", "path", "vf", "vt", "kind", "size", "c2", "c3", "c4", "us", "n_files"];
/// Class columns as a deployment names them (gcs: storage classes 2–4 are nearline, coldline, archive).
fn aliases(c: &str) -> &'static [&'static str] {
    match c {
        "c2" => &["nearline_bytes"],
        "c3" => &["coldline_bytes"],
        "c4" => &["archive_bytes"],
        _ => &[],
    }
}
const NO_PARENT: u32 = u32::MAX;

struct Input {
    path: PathBuf,
    meta: ArrowReaderMetadata,
    /// Projection indices of `COLS` in the file's root columns.
    proj: Vec<usize>,
    /// The file's name for each of `COLS`.
    names: Vec<String>,
    /// `(row group, depth min, depth max)`.
    groups: Vec<(usize, u8, u8)>,
}

struct Rec {
    h: u128,
    parent: u32,
    name: u32,
    vf: u16,
    vt: u16,
    file: bool,
    cls: u8,
    owner: u16,
    size: u64,
    /// A directory version's `n_files`.
    files: u64,
    cx: Option<Box<Complex>>,
}

#[derive(Clone, Copy)]
struct Ver {
    size: u64,
    vf: u16,
    vt: u16,
    owner: u16,
    flags: u8,
}

/// Distinct names, interned concurrently; temporary ids until the dictionary is sorted.
struct Interner {
    shards: Vec<Mutex<(IdMap<u128, u32>, Vec<(u32, Box<str>)>)>>,
    next: AtomicU32,
}

impl Interner {
    fn new() -> Interner {
        Interner { shards: (0..256).map(|_| Mutex::new((IdMap::default(), Vec::new()))).collect(), next: AtomicU32::new(0) }
    }
    fn intern(&self, s: &str) -> u32 {
        let h = h128(s.as_bytes());
        let mut sh = self.shards[(h >> 120) as usize].lock().unwrap();
        if let Some(&id) = sh.0.get(&h) {
            return id;
        }
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        sh.0.insert(h, id);
        sh.1.push((id, Box::from(s)));
        id
    }
    fn into_names(self) -> Vec<Box<str>> {
        let n = self.next.load(Ordering::Relaxed) as usize;
        let mut out: Vec<Box<str>> = vec![Box::from(""); n];
        for sh in self.shards {
            for (id, s) in sh.into_inner().unwrap().1 {
                out[id as usize] = s;
            }
        }
        out
    }
}

struct Owners(Mutex<HashMap<String, u16>>);

impl Owners {
    fn id(&self, name: &str) -> Result<u16> {
        let mut m = self.0.lock().unwrap();
        if let Some(&i) = m.get(name) {
            return Ok(i);
        }
        let i = m.len();
        if i >= u16::MAX as usize - 1 {
            return err("more owners than a u16 holds");
        }
        m.insert(name.to_string(), i as u16);
        Ok(i as u16)
    }
}

fn stat_range(s: Option<&Statistics>) -> Option<(u8, u8)> {
    match s? {
        Statistics::Int32(v) => Some((*v.min_opt()? as u8, *v.max_opt()? as u8)),
        Statistics::Int64(v) => Some((*v.min_opt()? as u8, *v.max_opt()? as u8)),
        _ => None,
    }
}

fn open_input(path: PathBuf) -> Result<Input> {
    let f = File::open(&path)?;
    let meta = ArrowReaderMetadata::load(&f, ArrowReaderOptions::new())?;
    let schema = meta.schema().clone();
    let mut proj = Vec::new();
    let mut names = Vec::new();
    for c in COLS {
        let found = std::iter::once(c).chain(aliases(c).iter().copied()).find(|n| schema.index_of(n).is_ok());
        let n = found.ok_or_else(|| format!("{}: no column `{c}`", path.display()))?;
        proj.push(schema.index_of(n)?);
        names.push(n.to_string());
    }
    let md = meta.metadata().clone();
    let leaf = md.file_metadata().schema_descr().columns().iter().position(|c| c.name() == "depth").ok_or("no depth leaf")?;
    let groups = (0..md.num_row_groups())
        .map(|g| {
            let (lo, hi) = stat_range(md.row_group(g).column(leaf).statistics()).unwrap_or((0, u8::MAX));
            (g, lo, hi)
        })
        .collect();
    Ok(Input { path, meta, proj, names, groups })
}

/// Owner slices of a file version (`interval_store.us_sql_from_raw`'s `us`): `''` → unnamed; a name → that owner; JSON
/// → its entries, plus the unnamed rest when positive.
fn owner_slices(us: &str, size: u64, owners: &Owners) -> Result<Vec<(u16, u64)>> {
    if us.is_empty() {
        return Ok(vec![(0, size)]);
    }
    if !us.starts_with('[') {
        return Ok(vec![(owners.id(us)?, size)]);
    }
    let v: Vec<(String, i64)> = serde_json::from_str(us).map_err(|e| format!("us {us:?}: {e}"))?;
    let mut out = Vec::with_capacity(v.len() + 1);
    let mut named = 0u64;
    for (u, b) in v {
        let b = b.max(0) as u64;
        named += b;
        out.push((owners.id(&u)?, b));
    }
    if size > named {
        out.push((0, size - named));
    }
    Ok(out)
}

/// Class slices: `c2..c4` where positive, class 1 the rest when positive; none at all → class 1 with nothing.
fn class_slices(size: u64, c: [i64; 3]) -> (u8, [u64; 4]) {
    let mut cls = [0u64; 4];
    let mut mask = 0u8;
    let mut rest = size as i64;
    for (k, &v) in c.iter().enumerate() {
        if v > 0 {
            cls[k + 1] = v as u64;
            mask |= 1 << (k + 1);
            rest -= v;
        }
    }
    if rest > 0 {
        cls[0] = rest as u64;
        mask |= 1;
    }
    if mask == 0 {
        mask = 1;
    }
    (mask, cls)
}

#[allow(clippy::too_many_arguments)]
fn read_level(
    inp: &Input,
    rg: usize,
    d: u8,
    prev: &(Vec<u128>, u32),
    names: &Interner,
    owners: &Owners,
    ords: &HashMap<i64, u16>,
) -> Result<Vec<Rec>> {
    let f = File::open(&inp.path)?;
    let b = ParquetRecordBatchReaderBuilder::new_with_metadata(f, inp.meta.clone());
    let mask = ProjectionMask::roots(b.parquet_schema(), inp.proj.iter().copied());
    let reader = b.with_row_groups(vec![rg]).with_projection(mask).with_batch_size(65536).build()?;
    let mut out = Vec::new();
    let mut cache: IdMap<u128, u32> = IdMap::default();
    let (mut last_parent, mut last_parent_id): (String, u32) = (String::new(), NO_PARENT);
    for batch in reader {
        let batch: RecordBatch = batch?;
        let col = |name: &str| {
            let n = &inp.names[COLS.iter().position(|c| *c == name).unwrap()];
            batch.column(batch.schema().index_of(n).unwrap()).clone()
        };
        let (depth, path, kind, us) = (col("depth"), col("path"), col("kind"), col("us"));
        let depth = ints(&depth)?;
        let (path, kind, us) = (Strs::of(&path)?, Strs::of(&kind)?, Strs::of(&us)?);
        let (vf, vt, size) = (ints(&col("vf"))?, ints(&col("vt"))?, ints(&col("size"))?);
        let (c2, c3, c4) = (ints(&col("c2"))?, ints(&col("c3"))?, ints(&col("c4"))?);
        let n_files = ints(&col("n_files"))?;
        let ord = |t: i64| -> Result<u16> {
            ords.get(&t).copied().ok_or_else(|| format!("{}: stamp {t} is no scan's", inp.path.display()).into())
        };
        for i in 0..depth.len() {
            if depth[i] != d as i64 {
                continue;
            }
            let p = path.get(i);
            let (parent_s, name) = match p.rfind('/') {
                Some(j) => (&p[..j], &p[j + 1..]),
                None => ("", p),
            };
            let parent = if d == 1 {
                NO_PARENT
            } else if parent_s == last_parent {
                last_parent_id
            } else {
                let ph = h128(parent_s.as_bytes());
                let id = prev.0.binary_search(&ph).map_err(|_| format!("{p}: its parent has no row"))? as u32 + prev.1;
                last_parent = parent_s.to_string();
                last_parent_id = id;
                id
            };
            let nh = h128(name.as_bytes());
            let name_id = match cache.get(&nh) {
                Some(&id) => id,
                None => {
                    let id = names.intern(name);
                    if cache.len() > 1 << 16 {
                        cache.clear();
                    }
                    cache.insert(nh, id);
                    id
                }
            };
            let file = match kind.get(i) {
                "file" => true,
                "dir" => false,
                k => return err(format!("{p}: kind {k:?}")),
            };
            let mut r = Rec { h: h128(p.as_bytes()), parent, name: name_id, vf: ord(vf[i])?, vt: ord(vt[i])?, file, cls: 0, owner: 0, size: 0, files: 0, cx: None };
            if !file {
                r.size = size[i].max(0) as u64;
                r.files = n_files[i].max(0) as u64;
            } else {
                let sz = size[i].max(0) as u64;
                r.size = sz;
                let os = owner_slices(us.get(i), sz, owners)?;
                let (mask, cls) = class_slices(sz, [c2[i], c3[i], c4[i]]);
                let simple_owner = os.len() == 1 && os[0].1 == sz;
                let simple_cls = mask.count_ones() == 1 && cls[mask.trailing_zeros() as usize] == sz;
                if simple_owner && simple_cls {
                    r.owner = os[0].0;
                    r.cls = mask.trailing_zeros() as u8;
                } else {
                    r.cx = Some(Box::new(Complex { owners: os, cls_mask: mask, cls }));
                }
            }
            out.push(r);
        }
    }
    Ok(out)
}

/// One chunk of a sorted level: its nodes (in order) and their versions.
#[derive(Default)]
struct Walk {
    parent: Vec<u32>,
    name: Vec<u32>,
    n_ver: Vec<u16>,
    hashes: Vec<u128>,
    vers: Vec<Ver>,
    cxs: Vec<(u64, Box<Complex>)>,
    dir_vf: Vec<u16>,
    dir_vt: Vec<u16>,
    dir_size: Vec<u64>,
    dir_files: Vec<u64>,
    dir_read: u64,
    file_read: u64,
    combined: u64,
}

fn walk(recs: &mut [Rec], n_scans: u16) -> Result<Walk> {
    let mut w = Walk::default();
    let mut i = 0;
    while i < recs.len() {
        let h = recs[i].h;
        w.parent.push(recs[i].parent);
        w.name.push(recs[i].name);
        w.hashes.push(h);
        let mut k = 0u32;
        let mut j = i;
        // Directory versions: one structural row (`size`: its first version's index), its versions (combined,
        // smallest `vt`) aside.
        if !recs[j].file {
            let vf = recs[j].vf;
            let first = w.dir_vf.len();
            while j < recs.len() && recs[j].h == h && !recs[j].file {
                w.dir_read += 1;
                let r = &recs[j];
                let n = w.dir_vf.len();
                if n > first && w.dir_vf[n - 1] == r.vf {
                    w.dir_vt[n - 1] = w.dir_vt[n - 1].min(r.vt);
                    w.combined += 1;
                } else {
                    w.dir_vf.push(r.vf);
                    w.dir_vt.push(r.vt);
                    w.dir_size.push(r.size);
                    w.dir_files.push(r.files);
                }
                j += 1;
            }
            let nd = w.dir_vf.len() - first;
            if nd > u16::MAX as usize {
                return err("a directory with more versions than a u16 holds");
            }
            w.vers.push(Ver { size: first as u64, vf, vt: n_scans, owner: nd as u16, flags: 0 });
            k += 1;
        }
        while j < recs.len() && recs[j].h == h {
            w.file_read += 1;
            let (vf, mut vt) = (recs[j].vf, recs[j].vt);
            let mut z = j + 1;
            while z < recs.len() && recs[z].h == h && recs[z].vf == vf {
                vt = vt.min(recs[z].vt);
                w.combined += 1;
                w.file_read += 1;
                z += 1;
            }
            let r = &mut recs[j];
            let mut flags = FILE | (r.cls << CLS_SHIFT);
            if let Some(cx) = r.cx.take() {
                flags |= COMPLEX;
                w.cxs.push((w.vers.len() as u64, cx));
            }
            w.vers.push(Ver { size: r.size, vf, vt, owner: r.owner, flags });
            k += 1;
            j = z;
        }
        if k > u16::MAX as u32 {
            return err("a path with more versions than a u16 holds");
        }
        w.n_ver.push(k as u16);
        i = j;
    }
    Ok(w)
}

fn rss_peak_gb() -> f64 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| s.lines().find(|l| l.starts_with("VmHWM:")).and_then(|l| l.split_whitespace().nth(1)?.parse::<f64>().ok()))
        .map(|kb| kb / 1024.0 / 1024.0)
        .unwrap_or(0.0)
}

pub fn load(gen: &Generation) -> Result<Index> {
    let t0 = Instant::now();
    let mut stats = LoadStats { files: gen.files.len(), ..Default::default() };
    let lap = |name: &str, t: &mut Instant, stats: &mut LoadStats| {
        stats.secs.push((name.to_string(), t.elapsed().as_secs_f64()));
        *t = Instant::now();
    };
    let mut t = Instant::now();
    let n_scans = gen.scans.len() as u16;
    let mut ords: HashMap<i64, u16> = gen.scans.iter().enumerate().map(|(i, s)| (s.ts, i as u16)).collect();
    ords.insert(OPEN, n_scans);
    let inputs: Vec<Input> = gen.files.iter().map(|p| open_input(p.clone())).collect::<Result<_>>()?;
    let names = Interner::new();
    let owners = Owners(Mutex::new(HashMap::from([(String::new(), 0u16)])));

    // Nodes (level by level) and their versions, in node order.
    // Capacities at their upper bound (every row a node / a version): pages never written stay unresident, and no
    // doubling realloc copies a multi-GB vector mid-load.
    let bound: usize = inputs.iter().map(|i| i.meta.metadata().file_metadata().num_rows() as usize).sum();
    let mut parent: Vec<u32> = Vec::with_capacity(bound);
    let mut name: Vec<u32> = Vec::with_capacity(bound);
    let mut n_ver: Vec<u16> = Vec::with_capacity(bound);
    let mut level_start: Vec<usize> = vec![0, 0];
    let mut vers: Vec<Ver> = Vec::with_capacity(bound);
    // Directory versions, as the index keeps them (no copy at the end).
    let (mut dir_vf, mut dir_vt): (Vec<u16>, Vec<u16>) = (Vec::with_capacity(bound), Vec::with_capacity(bound));
    let (mut dir_size, mut dir_files): (Vec<u64>, Vec<u64>) = (Vec::with_capacity(bound), Vec::with_capacity(bound));
    let mut cxs: Vec<(u64, Box<Complex>)> = Vec::new();
    // The previous level's paths: sorted hashes, and the first node id.
    let mut prev: (Vec<u128>, u32) = (Vec::new(), 0);
    let mut d = 1u8;
    loop {
        let todo: Vec<(usize, usize)> = inputs
            .iter()
            .enumerate()
            .flat_map(|(fi, inp)| inp.groups.iter().filter(|g| g.1 <= d && d <= g.2).map(move |g| (fi, g.0)))
            .collect();
        let parts: Vec<Vec<Rec>> = todo.par_iter().map(|&(fi, g)| read_level(&inputs[fi], g, d, &prev, &names, &owners, &ords)).collect::<Result<_>>()?;
        let mut recs: Vec<Rec> = Vec::with_capacity(parts.iter().map(|p| p.len()).sum());
        for p in parts {
            recs.extend(p);
        }
        if recs.is_empty() {
            // Every row's parent has a row, so an empty level has nothing below it.
            break;
        }
        stats.versions_read += recs.len() as u64;
        recs.par_sort_unstable_by(|a, b| (a.h, a.file, a.vf).cmp(&(b.h, b.file, b.vf)));
        // Walk the sorted level in chunks (cut where the path changes), each into its own nodes and versions, then
        // append them in order: node ids stay in hash order, so the level's map is its sorted hashes.
        let n = recs.len();
        let step = (n / (rayon::current_num_threads() * 4)).max(1 << 16);
        let mut cuts = vec![0usize];
        let mut c = step;
        while c < n {
            while c < n && recs[c].h == recs[c - 1].h {
                c += 1;
            }
            if c < n {
                cuts.push(c);
            }
            c += step;
        }
        cuts.push(n);
        let mut slices: Vec<&mut [Rec]> = Vec::with_capacity(cuts.len());
        let mut rest: &mut [Rec] = &mut recs[..];
        for w in cuts.windows(2) {
            let (head, tail) = rest.split_at_mut(w[1] - w[0]);
            slices.push(head);
            rest = tail;
        }
        let walked: Vec<Walk> = slices.into_par_iter().map(|sl| walk(sl, n_scans)).collect::<Result<_>>()?;
        drop(recs);
        let mut hashes: Vec<u128> = Vec::with_capacity(walked.iter().map(|w| w.hashes.len()).sum());
        for w in walked {
            let (v0, d0) = (vers.len() as u64, dir_vf.len() as u64);
            stats.dir_versions_read += w.dir_read;
            stats.file_versions_read += w.file_read;
            stats.combined += w.combined;
            parent.extend_from_slice(&w.parent);
            name.extend_from_slice(&w.name);
            n_ver.extend_from_slice(&w.n_ver);
            hashes.extend_from_slice(&w.hashes);
            vers.extend(w.vers.iter().map(|v| if v.flags & FILE == 0 { Ver { size: v.size + d0, ..*v } } else { *v }));
            cxs.extend(w.cxs.into_iter().map(|(i, c)| (i + v0, c)));
            dir_vf.extend_from_slice(&w.dir_vf);
            dir_vt.extend_from_slice(&w.dir_vt);
            dir_size.extend_from_slice(&w.dir_size);
            dir_files.extend_from_slice(&w.dir_files);
        }
        let base = level_start[level_start.len() - 1] as u32;
        level_start.push(parent.len());
        prev = (hashes, base);
        stats.max_depth = d;
        if d == u8::MAX {
            break;
        }
        d += 1;
    }
    drop(prev);
    lap("read levels", &mut t, &mut stats);

    // The dictionary: sorted by bytes, then each id's rank in `name + "/"` order.
    let raw = names.into_names();
    let mut order: Vec<u32> = (0..raw.len() as u32).collect();
    order.par_sort_unstable_by(|&a, &b| raw[a as usize].as_bytes().cmp(raw[b as usize].as_bytes()));
    let mut final_id = vec![0u32; raw.len()];
    for (f, &tmp) in order.iter().enumerate() {
        final_id[tmp as usize] = f as u32;
    }
    // Names by final id, borrowed (no copies).
    let sorted = |f: u32| raw[order[f as usize] as usize].as_bytes();
    let mut by_slash: Vec<u32> = (0..raw.len() as u32).collect();
    by_slash.par_sort_unstable_by(|&a, &b| cmp_slash(sorted(a), sorted(b)));
    let mut slash_rank = vec![0u32; raw.len()];
    for (r, &id) in by_slash.iter().enumerate() {
        slash_rank[id as usize] = r as u32;
    }
    drop(by_slash);
    let dict = Dict::build_iter(order.iter().map(|&tmp| &*raw[tmp as usize]), raw.len());
    stats.names = raw.len() as u64;
    drop(order);
    drop(raw);
    name.par_iter_mut().for_each(|x| *x = final_id[*x as usize]);
    drop(final_id);

    // Owners, sorted by name ('' first).
    let om = owners.0.into_inner().unwrap();
    let mut onames: Vec<(String, u16)> = om.into_iter().collect();
    onames.sort();
    let mut oremap = vec![0u16; onames.len()];
    for (f, (_, tmp)) in onames.iter().enumerate() {
        oremap[*tmp as usize] = f as u16;
    }
    let owner_names: Vec<String> = onames.into_iter().map(|(n, _)| n).collect();
    stats.owners = owner_names.len() as u64;
    lap("dictionary", &mut t, &mut stats);

    // Depth-first positions: subtree sizes bottom-up, then each level ordered by (parent's position, name + "/").
    let nodes = parent.len();
    let levels = level_start.len() - 2;
    if stats.versions_read >= u32::MAX as u64 {
        return err("more versions than a u32 row id holds");
    }
    // Rows ≤ versions read < 2^32, so a subtree's row count fits a u32.
    let subtree: Vec<u32> = n_ver.par_iter().map(|&k| k as u32).collect();
    {
        // Bottom-up, a level at a time: each node adds into its parent (one level up, so reads and writes never meet).
        let sub: &[AtomicU32] = unsafe { std::slice::from_raw_parts(subtree.as_ptr() as *const AtomicU32, subtree.len()) };
        for lv in (2..=levels).rev() {
            (level_start[lv]..level_start[lv + 1]).into_par_iter().for_each(|x| {
                let v = sub[x].load(Ordering::Relaxed);
                sub[parent[x] as usize].fetch_add(v, Ordering::Relaxed);
            });
        }
    }
    let total: u64 = (level_start[1]..level_start[2]).map(|x| subtree[x] as u64).sum();
    if total >= u32::MAX as u64 {
        return err("more rows than a u32 row id holds");
    }
    let mut pos = vec![0u32; nodes];
    for lv in 1..=levels {
        // Sort the level by (parent's position + 1, name's rank in `name + "/"` order); the root's children have 0.
        let mut keyed: Vec<(u64, u32)> = (level_start[lv] as u32..level_start[lv + 1] as u32)
            .into_par_iter()
            .map(|x| {
                let p = parent[x as usize];
                let hi = if p == NO_PARENT { 0 } else { pos[p as usize] as u64 + 1 };
                ((hi << 32) | slash_rank[name[x as usize] as usize] as u64, x)
            })
            .collect();
        keyed.par_sort_unstable();
        // Positions: each parent's children follow its own rows, in order. Chunks start at a parent's first child, so
        // each chunk's running offsets are its own.
        let n = keyed.len();
        let mut cuts = vec![0usize];
        let step = (n / (rayon::current_num_threads() * 4)).max(1 << 16);
        let mut c = step;
        while c < n {
            while c < n && keyed[c].0 >> 32 == keyed[c - 1].0 >> 32 {
                c += 1;
            }
            if c < n {
                cuts.push(c);
            }
            c += step;
        }
        cuts.push(n);
        let pw = Shared(pos.as_mut_ptr());
        let pos_r: &[u32] = unsafe { std::slice::from_raw_parts(pos.as_ptr(), nodes) };
        cuts.par_windows(2).for_each(|w| {
            let (mut cur, mut off) = (u64::MAX, 0u64);
            for &(k, x) in &keyed[w[0]..w[1]] {
                let hi = k >> 32;
                if hi != cur {
                    cur = hi;
                    off = if hi == 0 { 0 } else { pos_r[parent[x as usize] as usize] as u64 + n_ver[parent[x as usize] as usize] as u64 };
                }
                // Safety: each node is written once, by the chunk holding it; reads are of the level above.
                unsafe { pw.write(x as usize, off as u32) };
                off += subtree[x as usize] as u64;
            }
        });
    }
    drop(slash_rank);
    lap("order", &mut t, &mut stats);

    // Scatter every version to its row.
    let n = total as usize;
    let mut first_ver = vec![0u64; nodes + 1];
    for x in 0..nodes {
        first_ver[x + 1] = first_ver[x] + n_ver[x] as u64;
    }
    let mut node_depth = vec![0u8; nodes];
    for lv in 1..=levels {
        node_depth[level_start[lv]..level_start[lv + 1]].fill(lv as u8);
    }
    let mut c_name = vec![0u32; n];
    let mut c_depth = vec![0u8; n];
    let mut c_end = vec![0u32; n];
    let mut c_flags = vec![0u8; n];
    let mut c_size = vec![0u64; n];
    let mut c_vf = vec![0u16; n];
    let mut c_vt = vec![0u16; n];
    let mut c_owner = vec![0u16; n];
    let ptrs = (
        Shared(c_name.as_mut_ptr()),
        Shared(c_depth.as_mut_ptr()),
        Shared(c_end.as_mut_ptr()),
        Shared(c_flags.as_mut_ptr()),
        Shared(c_size.as_mut_ptr()),
        Shared(c_vf.as_mut_ptr()),
        Shared(c_vt.as_mut_ptr()),
        Shared(c_owner.as_mut_ptr()),
    );
    let cx_index: Vec<u64> = cxs.iter().map(|c| c.0).collect();
    let found: Vec<(Vec<(u32, usize)>, Vec<(u32, u32)>)> = (0..nodes)
        .into_par_iter()
        .chunks(1 << 16)
        .map(|xs| {
            let mut cx_rows = Vec::new();
            let mut cks = Vec::new();
            for x in xs {
                let p = pos[x] as usize;
                let e = (p as u64 + subtree[x] as u64 - 1) as u32;
                let k = n_ver[x] as usize;
                for v in 0..k {
                    let vi = first_ver[x] as usize + v;
                    let ver = vers[vi];
                    let row = p + v;
                    let mut flags = ver.flags;
                    if v > 0 {
                        flags |= CONT;
                    }
                    if flags & COMPLEX != 0 {
                        cx_rows.push((row as u32, cx_index.binary_search(&(vi as u64)).unwrap()));
                    }
                    unsafe {
                        ptrs.0.write(row, name[x]);
                        ptrs.1.write(row, node_depth[x]);
                        ptrs.2.write(row, e);
                        ptrs.3.write(row, flags);
                        ptrs.4.write(row, ver.size);
                        ptrs.5.write(row, ver.vf);
                        ptrs.6.write(row, ver.vt);
                        ptrs.7.write(row, if flags & FILE != 0 { oremap[ver.owner as usize] } else { ver.owner });
                    }
                }
                let mut c = p.div_ceil(CK) * CK;
                while c < p + k {
                    cks.push((c as u32, x as u32));
                    c += CK;
                }
            }
            (cx_rows, cks)
        })
        .collect();
    let mut cx_rows: Vec<(u32, usize)> = Vec::new();
    let mut cks: Vec<(u32, u32)> = Vec::new();
    for (a, b) in found {
        cx_rows.extend(a);
        cks.extend(b);
    }
    cx_rows.sort_unstable();
    let mut cx_slot: Vec<Option<Box<Complex>>> = cxs.into_iter().map(|c| Some(c.1)).collect();
    let mut complex_rows = Vec::with_capacity(cx_rows.len());
    let mut complex = Vec::with_capacity(cx_rows.len());
    for (row, i) in cx_rows {
        let mut c = *cx_slot[i].take().unwrap();
        for o in c.owners.iter_mut() {
            o.0 = oremap[o.0 as usize];
        }
        complex_rows.push(row);
        complex.push(c);
    }
    drop(vers);
    lap("scatter", &mut t, &mut stats);

    // Checkpoints: each multiple of CK's ancestor chain.
    cks.sort_unstable();
    if cks.len() != n.div_ceil(CK) {
        return err(format!("{} checkpoints for {n} rows", cks.len()));
    }
    let mut ck_off = vec![0u64];
    let mut ck_rows = Vec::new();
    for (c, x) in cks {
        let mut chain = Vec::new();
        if pos[x as usize] != c {
            chain.push(pos[x as usize]);
        }
        let mut a = parent[x as usize];
        while a != NO_PARENT {
            chain.push(pos[a as usize]);
            a = parent[a as usize];
        }
        chain.reverse();
        ck_rows.extend(chain);
        ck_off.push(ck_rows.len() as u64);
    }
    lap("checkpoints", &mut t, &mut stats);

    stats.nodes = nodes as u64;
    stats.rows = n as u64;
    stats.file_rows = c_flags.par_iter().filter(|&&f| f & FILE != 0).count() as u64;
    stats.dir_rows = stats.rows - stats.file_rows;
    stats.complex_rows = complex_rows.len() as u64;
    stats.secs.push(("total".to_string(), t0.elapsed().as_secs_f64()));
    stats.peak_rss_gb = rss_peak_gb();
    stats.dir_versions = dir_vf.len() as u64;
    dir_vf.shrink_to_fit();
    dir_vt.shrink_to_fit();
    dir_size.shrink_to_fit();
    dir_files.shrink_to_fit();
    Ok(Index {
        dir_vf,
        dir_vt,
        dir_size,
        dir_files,
        scans: gen.scans.clone(),
        name: c_name,
        depth: c_depth,
        end: c_end,
        flags: c_flags,
        size: c_size,
        vf: c_vf,
        vt: c_vt,
        owner: c_owner,
        complex_rows,
        complex,
        owners: owner_names,
        dict,
        ck_off,
        ck_rows,
        stats,
    })
}
