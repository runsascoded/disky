//! The packed in-memory form as one file, mapped back without parsing (a Batch step writes it once; a server maps it).
//!
//! Layout (version 1):
//! - bytes 0..8: magic `FSBPACK\0`; 8..16: the JSON header's length (u64 LE); then the header (≤ `HEADER_MAX`);
//! - the header: `{"version": 1, "kind": "full" | "slim", "meta": {…}, "sections": [{"name", "elem", "offset", "len"}]}`
//!   (`elem` one of `u8`/`u16`/`u32`/`u64`, `len` in elements);
//! - each section's raw little-endian elements at a 4 KiB-aligned offset, so every column maps as a typed slice.
//!
//! A reader refuses another magic or version, a section whose element type differs, and a file shorter than its sections.

use std::collections::HashMap;
use std::fs::File;
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::Arc;
use std::time::Instant;

use memmap2::Mmap;
use rayon::prelude::*;
use serde_json::{json, Value};

use crate::col::Col;
use crate::dict::Dict;
use crate::index::{Complex, Index, LoadStats};
use crate::util::{err, Result};

pub const MAGIC: &[u8; 8] = b"FSBPACK\0";
pub const VERSION: u64 = 1;
const HEADER_MAX: u64 = 1 << 20;
const ALIGN: u64 = 4096;

pub trait Elem: Copy + 'static {
    const NAME: &'static str;
}
impl Elem for u8 {
    const NAME: &'static str = "u8";
}
impl Elem for u16 {
    const NAME: &'static str = "u16";
}
impl Elem for u32 {
    const NAME: &'static str = "u32";
}
impl Elem for u64 {
    const NAME: &'static str = "u64";
}

/// Sections to write, borrowed from the index being packed.
#[derive(Default)]
pub struct Writer<'a> {
    secs: Vec<(String, &'static str, usize, &'a [u8])>,
}

fn as_bytes<T: Elem>(v: &[T]) -> &[u8] {
    unsafe { std::slice::from_raw_parts(v.as_ptr() as *const u8, std::mem::size_of_val(v)) }
}

impl<'a> Writer<'a> {
    pub fn add<T: Elem>(&mut self, name: &str, v: &'a [T]) {
        self.secs.push((name.to_string(), T::NAME, v.len(), as_bytes(v)));
    }

    /// Write to `path` (via `<path>.tmp`, renamed when synced). Returns the file's size.
    pub fn write(&self, path: &Path, kind: &str, meta: Value) -> Result<u64> {
        let mut off = HEADER_MAX;
        let mut table = Vec::new();
        for (name, elem, len, b) in &self.secs {
            off = off.div_ceil(ALIGN) * ALIGN;
            table.push(json!({"name": name, "elem": elem, "offset": off, "len": len}));
            off += b.len() as u64;
        }
        let header = serde_json::to_vec(&json!({"version": VERSION, "kind": kind, "meta": meta, "sections": table}))?;
        if 16 + header.len() as u64 > HEADER_MAX {
            return err(format!("pack header of {} bytes", header.len()));
        }
        let tmp = path.with_extension("tmp");
        let f = File::create(&tmp)?;
        let mut w = BufWriter::with_capacity(8 << 20, f);
        w.write_all(MAGIC)?;
        w.write_all(&(header.len() as u64).to_le_bytes())?;
        w.write_all(&header)?;
        let mut pos = 16 + header.len() as u64;
        for (t, (_, _, _, b)) in table.iter().zip(&self.secs) {
            let o = t["offset"].as_u64().unwrap();
            if o > pos {
                w.flush()?;
                w.get_mut().seek(SeekFrom::Start(o))?;
            }
            w.write_all(b)?;
            pos = o + b.len() as u64;
        }
        w.flush()?;
        let f = w.into_inner().map_err(|e| e.to_string())?;
        f.set_len(off)?;
        f.sync_all()?;
        std::fs::rename(&tmp, path)?;
        Ok(off)
    }
}

/// A mapped pack file.
pub struct Pack {
    pub map: Arc<Mmap>,
    pub kind: String,
    pub meta: Value,
    secs: HashMap<String, (String, u64, usize)>,
}

impl Pack {
    pub fn open(path: &Path) -> Result<Pack> {
        let f = File::open(path)?;
        let map = unsafe { Mmap::map(&f)? };
        if map.len() < 16 || &map[..8] != MAGIC {
            return err(format!("{}: not a pack file", path.display()));
        }
        let hl = u64::from_le_bytes(map[8..16].try_into().unwrap()) as usize;
        let h: Value = serde_json::from_slice(&map[16..16 + hl])?;
        if h["version"].as_u64() != Some(VERSION) {
            return err(format!("{}: pack version {}, this reader reads {VERSION}", path.display(), h["version"]));
        }
        let mut secs = HashMap::new();
        for s in h["sections"].as_array().ok_or("pack: no sections")? {
            let (name, elem) = (s["name"].as_str().unwrap().to_string(), s["elem"].as_str().unwrap().to_string());
            let (off, len) = (s["offset"].as_u64().unwrap(), s["len"].as_u64().unwrap() as usize);
            let size = match elem.as_str() {
                "u8" => 1,
                "u16" => 2,
                "u32" => 4,
                "u64" => 8,
                e => return err(format!("pack: element type {e}")),
            };
            if off % ALIGN != 0 || off as usize + len * size > map.len() {
                return err(format!("{}: section {name} out of the file", path.display()));
            }
            secs.insert(name, (elem, off, len));
        }
        Ok(Pack { kind: h["kind"].as_str().unwrap_or("").to_string(), meta: h["meta"].clone(), secs, map: Arc::new(map) })
    }

    pub fn col<T: Elem>(&self, name: &str) -> Result<Col<T>> {
        let (elem, off, len) = self.secs.get(name).ok_or_else(|| format!("pack: no section {name}"))?;
        if elem != T::NAME {
            return err(format!("pack: section {name} is {elem}, not {}", T::NAME));
        }
        if *len == 0 {
            return Ok(Col::Own(Vec::new()));
        }
        Ok(Col::Map(unsafe { self.map.as_ptr().add(*off as usize) } as *const T, *len))
    }

    pub fn len(&self) -> u64 {
        self.map.len() as u64
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }

    /// Fault every page in (`MADV_POPULATE_READ`, in 64 MiB pieces on `threads` threads): what the first query then
    /// doesn't wait for. Returns seconds.
    pub fn prefault(&self, threads: usize) -> Result<f64> {
        let t = Instant::now();
        let piece = 64usize << 20;
        let base = self.map.as_ptr() as usize;
        let n = self.map.len();
        let pool = rayon::ThreadPoolBuilder::new().num_threads(threads).build()?;
        let bad: usize = pool.install(|| {
            (0..n.div_ceil(piece))
                .into_par_iter()
                .map(|i| {
                    let (a, b) = (i * piece, ((i + 1) * piece).min(n));
                    let r = unsafe { libc::madvise((base + a) as *mut libc::c_void, b - a, 22 /* MADV_POPULATE_READ */) };
                    if r != 0 {
                        // An older kernel: touch one byte per page.
                        let mut s = 0u8;
                        for p in (a..b).step_by(4096) {
                            s = s.wrapping_add(unsafe { std::ptr::read_volatile((base + p) as *const u8) });
                        }
                        std::hint::black_box(s);
                        1
                    } else {
                        0
                    }
                })
                .sum()
        });
        if bad > 0 {
            eprintln!("prefault: MADV_POPULATE_READ refused on {bad} pieces; touched them");
        }
        Ok(t.elapsed().as_secs_f64())
    }

    pub fn dict(&self, p: &str) -> Result<Dict> {
        Ok(Dict {
            lower: self.col(&format!("{p}lower"))?,
            offs: self.col(&format!("{p}offs"))?,
            patch_ids: self.col(&format!("{p}patch_ids"))?,
            patch_offs: self.col(&format!("{p}patch_offs"))?,
            patch_buf: self.col(&format!("{p}patch_buf"))?,
        })
    }

    pub fn complex(&self) -> Result<Vec<Complex>> {
        let off: Col<u32> = self.col("cx_owner_off")?;
        let ids: Col<u16> = self.col("cx_owner_id")?;
        let bytes: Col<u64> = self.col("cx_owner_bytes")?;
        let mask: Col<u8> = self.col("cx_cls_mask")?;
        let cls: Col<u64> = self.col("cx_cls")?;
        Ok((0..mask.len())
            .map(|i| Complex {
                owners: (off[i] as usize..off[i + 1] as usize).map(|k| (ids[k], bytes[k])).collect(),
                cls_mask: mask[i],
                cls: [cls[4 * i], cls[4 * i + 1], cls[4 * i + 2], cls[4 * i + 3]],
            })
            .collect())
    }
}

/// The dictionary's sections, under prefix `p`.
pub fn add_dict<'a>(w: &mut Writer<'a>, p: &str, d: &'a Dict) {
    w.add(&format!("{p}lower"), &d.lower);
    w.add(&format!("{p}offs"), &d.offs);
    w.add(&format!("{p}patch_ids"), &d.patch_ids);
    w.add(&format!("{p}patch_offs"), &d.patch_offs);
    w.add(&format!("{p}patch_buf"), &d.patch_buf);
}

/// The complex rows' slices flattened (owned: built for the write).
pub struct ComplexCols {
    off: Vec<u32>,
    ids: Vec<u16>,
    bytes: Vec<u64>,
    mask: Vec<u8>,
    cls: Vec<u64>,
}

impl ComplexCols {
    pub fn of(cx: &[Complex]) -> ComplexCols {
        let mut c = ComplexCols { off: vec![0], ids: vec![], bytes: vec![], mask: vec![], cls: vec![] };
        for x in cx {
            for &(o, b) in &x.owners {
                c.ids.push(o);
                c.bytes.push(b);
            }
            c.off.push(c.ids.len() as u32);
            c.mask.push(x.cls_mask);
            c.cls.extend_from_slice(&x.cls);
        }
        c
    }

    pub fn add<'a>(&'a self, w: &mut Writer<'a>) {
        w.add("cx_owner_off", &self.off);
        w.add("cx_owner_id", &self.ids);
        w.add("cx_owner_bytes", &self.bytes);
        w.add("cx_cls_mask", &self.mask);
        w.add("cx_cls", &self.cls);
    }
}

/// Write the full index (every column the loader builds) to `path`.
pub fn write_index(idx: &Index, path: &Path) -> Result<u64> {
    let mut w = Writer::default();
    w.add("name", &idx.name);
    w.add("depth", &idx.depth);
    w.add("end", &idx.end);
    w.add("flags", &idx.flags);
    w.add("size", &idx.size);
    w.add("vf", &idx.vf);
    w.add("vt", &idx.vt);
    w.add("owner", &idx.owner);
    w.add("dir_vf", &idx.dir_vf);
    w.add("dir_vt", &idx.dir_vt);
    w.add("dir_size", &idx.dir_size);
    w.add("dir_files", &idx.dir_files);
    w.add("complex_rows", &idx.complex_rows);
    w.add("ck_off", &idx.ck_off);
    w.add("ck_rows", &idx.ck_rows);
    add_dict(&mut w, "dict_", &idx.dict);
    let cx = ComplexCols::of(&idx.complex);
    cx.add(&mut w);
    w.write(path, "full", json!({"scans": idx.scans, "owners": idx.owners, "stats": idx.stats}))
}

/// The full index over a mapped pack (nothing read until a query touches it).
pub fn read_index(p: &Pack) -> Result<Index> {
    if p.kind != "full" {
        return err(format!("pack kind {}, not full", p.kind));
    }
    Ok(Index {
        scans: serde_json::from_value(p.meta["scans"].clone())?,
        owners: serde_json::from_value(p.meta["owners"].clone())?,
        stats: serde_json::from_value::<LoadStats>(p.meta["stats"].clone())?,
        name: p.col("name")?,
        depth: p.col("depth")?,
        end: p.col("end")?,
        flags: p.col("flags")?,
        size: p.col("size")?,
        vf: p.col("vf")?,
        vt: p.col("vt")?,
        owner: p.col("owner")?,
        dir_vf: p.col("dir_vf")?,
        dir_vt: p.col("dir_vt")?,
        dir_size: p.col("dir_size")?,
        dir_files: p.col("dir_files")?,
        complex_rows: p.col("complex_rows")?,
        complex: p.complex()?,
        ck_off: p.col("ck_off")?,
        ck_rows: p.col("ck_rows")?,
        dict: p.dict("dict_")?,
        map: Some(p.map.clone()),
    })
}

/// Process counters (Linux): bytes this process read from storage, major faults, seconds since it started, VmRSS and
/// VmHWM in GB. Zeros elsewhere.
#[derive(Clone, Copy, Debug, Default, serde::Serialize)]
pub struct Proc {
    pub read_bytes: u64,
    pub majflt: u64,
    pub since_start_s: f64,
    pub rss_gb: f64,
    pub hwm_gb: f64,
}

pub fn proc() -> Proc {
    let mut p = Proc::default();
    if let Ok(s) = std::fs::read_to_string("/proc/self/io") {
        p.read_bytes = s.lines().find_map(|l| l.strip_prefix("read_bytes: ")?.trim().parse().ok()).unwrap_or(0);
    }
    if let Ok(s) = std::fs::read_to_string("/proc/self/stat") {
        // Fields after the `(comm)`: state is field 3; majflt 12, starttime 22.
        let rest = &s[s.rfind(')').map_or(0, |i| i + 2)..];
        let f: Vec<&str> = rest.split_whitespace().collect();
        p.majflt = f.get(9).and_then(|x| x.parse().ok()).unwrap_or(0);
        let start: f64 = f.get(19).and_then(|x| x.parse().ok()).unwrap_or(0.0);
        let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) } as f64;
        let up: f64 = std::fs::read_to_string("/proc/uptime").ok().and_then(|u| u.split_whitespace().next()?.parse().ok()).unwrap_or(0.0);
        p.since_start_s = up - start / hz;
    }
    if let Ok(s) = std::fs::read_to_string("/proc/self/status") {
        let kb = |k: &str| s.lines().find_map(|l| l.strip_prefix(k)?.split_whitespace().next()?.parse::<f64>().ok()).unwrap_or(0.0);
        p.rss_gb = kb("VmRSS:") / 1048576.0;
        p.hwm_gb = kb("VmHWM:") / 1048576.0;
    }
    p
}
