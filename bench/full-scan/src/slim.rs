//! The slim index: object versions only, in depth-first path order, with the directory tree kept as structure beside
//! them, and every column its own group so a query reads only what it needs.
//!
//! **Object versions** (`n_o` rows; a path's versions together, by `vf`):
//! - validity + size, read for every row a pass sums: `vf`, `vt` (u16 scan ordinals), `size` (u64);
//! - `name` (u32 dictionary id): only a search reads it (and hydration, for the ≤ M emitted object nodes);
//! - `owner` (u16), `cls` (u8: class bits 2–3, `COMPLEX` bit 4, as the full index's flags): only owner / class filters;
//! - `cont`: one bit per row, "another version of the previous row's path": read only where object paths are nodes
//!   (depths in the view's window).
//!
//! **Directories** that hold at least one object version (`n_d` entries, pre-order): `ddepth` (u8), `dend` (u32: the
//! last directory of its subtree), `dlo`/`dhi` (u32: its subtree's object rows `[dlo, dhi)`), `dname` (u32: search and
//! hydration only). A path that is a directory in some scans and an object in others is one directory whose own object
//! versions lead its rows (`dself` lists those directories, sparse; the first own row's `cont` bit is clear, so a
//! directory's own rows are the `cont` run at `dlo`). Checkpoints every `CKD` directories hold the ancestor chain, for chunk seeds and paths. Directories'
//! rolled-up versions (`dir_*`) are dropped: every total is a sum over object versions.

use std::path::Path;
use std::time::Instant;

use serde_json::{json, Value};

use crate::col::Col;
use crate::dict::Dict;
use crate::index::{Complex, Index, COMPLEX, CONT, FILE};
use crate::pack::{add_dict, ComplexCols, Pack, Writer};
use crate::store::ScanInfo;
use crate::util::{err, Result};

/// Directories between ancestor-chain checkpoints.
pub const CKD: usize = 4096;

pub struct SlimIndex {
    pub scans: Vec<ScanInfo>,
    pub owners: Vec<String>,
    pub dict: Dict,
    pub vf: Col<u16>,
    pub vt: Col<u16>,
    pub size: Col<u64>,
    pub name: Col<u32>,
    pub owner: Col<u16>,
    pub cls: Col<u8>,
    pub cont: Col<u64>,
    pub complex_rows: Col<u32>,
    pub complex: Vec<Complex>,
    pub dname: Col<u32>,
    pub ddepth: Col<u8>,
    pub dend: Col<u32>,
    pub dlo: Col<u32>,
    pub dhi: Col<u32>,
    pub dck_off: Col<u64>,
    pub dck: Col<u32>,
    /// Directories whose own path also has object versions (sorted).
    pub dself: Col<u32>,
    pub stats: Value,
    pub map: Option<std::sync::Arc<crate::pack::Backing>>,
}

impl SlimIndex {
    pub fn n_o(&self) -> usize {
        self.vf.len()
    }

    pub fn n_d(&self) -> usize {
        self.ddepth.len()
    }

    #[inline(always)]
    pub fn is_cont(&self, r: usize) -> bool {
        unsafe { *self.cont.get_unchecked(r >> 6) >> (r & 63) & 1 != 0 }
    }

    pub fn scan(&self, id: &str) -> Option<u16> {
        self.scans.iter().position(|s| s.id == id).map(|i| i as u16)
    }

    /// Bytes per column group, the total, and per object version.
    pub fn column_bytes(&self) -> Value {
        let cx: u64 = self.complex.iter().map(|c| 48 + c.owners.len() as u64 * 16).sum::<u64>() + self.complex_rows.bytes();
        let groups = vec![
            ("validity (vf, vt)", self.vf.bytes() + self.vt.bytes()),
            ("size", self.size.bytes()),
            ("structure: object paths (cont bits)", self.cont.bytes()),
            ("structure: directories (ddepth, dend, dlo, dhi)", self.ddepth.bytes() + self.dend.bytes() + self.dlo.bytes() + self.dhi.bytes()),
            ("structure: checkpoints, dself", self.dck_off.bytes() + self.dck.bytes() + self.dself.bytes()),
            ("name: objects", self.name.bytes()),
            ("name: directories", self.dname.bytes()),
            ("dictionary: search buffer (lower)", self.dict.lower.bytes()),
            ("dictionary: hydration (offs, patches)", self.dict.offs.bytes() + self.dict.patch_ids.bytes() + self.dict.patch_offs.bytes() + self.dict.patch_buf.bytes()),
            ("owner", self.owner.bytes()),
            ("class (cls)", self.cls.bytes()),
            ("complex slices", cx),
        ];
        let total: u64 = groups.iter().map(|g| g.1).sum();
        let n = self.n_o().max(1) as f64;
        json!({
            "rows": self.n_o(), "dirs": self.n_d(), "names": self.dict.len(),
            "groups": groups.iter().map(|(k, b)| json!({"group": k, "bytes": b, "per_row": *b as f64 / n})).collect::<Vec<_>>(),
            "total": total, "per_row": total as f64 / n,
        })
    }

    /// The end of directory `j`'s own object rows (`dlo` when it has none).
    pub fn own_end(&self, j: usize) -> usize {
        let a = self.dlo[j] as usize;
        if self.dself.binary_search(&(j as u32)).is_err() {
            return a;
        }
        let mut e = a + 1;
        while e < self.n_o() && self.is_cont(e) {
            e += 1;
        }
        e
    }

    /// Directory `j`'s ancestors (directories whose subtree holds it), shallowest first.
    pub fn dir_chain(&self, j: usize) -> Vec<u32> {
        let c = j / CKD;
        let mut st: Vec<u32> = self.dck[self.dck_off[c] as usize..self.dck_off[c + 1] as usize].to_vec();
        for x in c * CKD..j {
            while st.last().is_some_and(|&g| (self.dend[g as usize] as usize) < x) {
                st.pop();
            }
            st.push(x as u32);
        }
        while st.last().is_some_and(|&g| (self.dend[g as usize] as usize) < j) {
            st.pop();
        }
        st
    }

    /// The directories holding object row `r` whose first object is at or before `r` and that are at or before
    /// directory `upto` (exclusive) in pre-order, shallowest first: ancestors-or-self of `upto − 1` whose rows reach
    /// past `r`.
    pub fn chain_before(&self, upto: usize, r: usize) -> Vec<u32> {
        if upto == 0 {
            return Vec::new();
        }
        let j = upto - 1;
        let mut ch = self.dir_chain(j);
        ch.push(j as u32);
        ch.retain(|&g| (self.dhi[g as usize] as usize) > r);
        ch
    }

    /// The directories holding object row `r`, shallowest first.
    pub fn chain_of_row(&self, r: usize) -> Vec<u32> {
        let upto = self.dlo.partition_point(|&x| (x as usize) <= r);
        self.chain_before(upto, r)
    }

    pub fn dir_path(&self, j: usize) -> String {
        let mut parts: Vec<&str> = self.dir_chain(j).iter().map(|&a| self.dict.orig(self.dname[a as usize])).collect();
        parts.push(self.dict.orig(self.dname[j]));
        parts.join("/")
    }

    pub fn obj_path(&self, r: usize) -> String {
        let mut parts: Vec<&str> = self.chain_of_row(r).iter().map(|&a| self.dict.orig(self.dname[a as usize])).collect();
        parts.push(self.dict.orig(self.name[r]));
        parts.join("/")
    }

    /// The object rows of `r`'s path: `[r', e)` with `r'` its first version.
    pub fn obj_rows(&self, r: usize) -> (usize, usize) {
        let mut a = r;
        while a > 0 && self.is_cont(a) {
            a -= 1;
        }
        let mut e = r + 1;
        while e < self.n_o() && self.is_cont(e) {
            e += 1;
        }
        (a, e)
    }

    /// `path`'s directory, or `None` for the root `""`; `Err` when it isn't a directory holding objects.
    pub fn find_dir(&self, path: &str) -> std::result::Result<Option<usize>, ()> {
        if path.is_empty() {
            return Ok(None);
        }
        let (mut a, mut b) = (0usize, self.n_d());
        let mut found = None;
        for seg in path.split('/') {
            let id = self.dict.find(seg).ok_or(())?;
            found = None;
            let mut k = a;
            while k < b {
                if self.dname[k] == id {
                    found = Some(k);
                    break;
                }
                k = self.dend[k] as usize + 1;
            }
            let g = found.ok_or(())?;
            a = g + 1;
            b = self.dend[g] as usize + 1;
        }
        Ok(found)
    }

    /// The object rows `[a, b)` at or under `path` (a directory, or an object path), None when it holds none.
    pub fn rows_of(&self, path: &str) -> Option<(usize, usize)> {
        if let Ok(d) = self.find_dir(path) {
            return Some(d.map_or((0, self.n_o()), |j| (self.dlo[j] as usize, self.dhi[j] as usize)));
        }
        let (parent, name) = match path.rsplit_once('/') {
            Some((p, n)) => (self.find_dir(p).ok()?, n),
            None => (None, path),
        };
        let id = self.dict.find(name)?;
        let (lo, hi, kids) = match parent {
            None => (0, self.n_o(), 0usize),
            Some(j) => (self.dlo[j] as usize, self.dhi[j] as usize, j + 1),
        };
        // The parent's direct objects: its rows outside its child directories.
        let mut k = kids;
        let end_d = parent.map_or(self.n_d(), |j| self.dend[j] as usize + 1);
        let mut r = lo;
        while r < hi {
            if k < end_d && self.dlo[k] as usize == r {
                r = self.dhi[k] as usize;
                k = self.dend[k] as usize + 1;
                continue;
            }
            if !self.is_cont(r) && self.name[r] == id {
                return Some(self.obj_rows(r));
            }
            r += 1;
        }
        None
    }

    /// The slim form of a full index: its object versions and the directories holding any, in the same order.
    pub fn from_index(idx: &Index) -> Result<SlimIndex> {
        let t0 = Instant::now();
        let n = idx.n();
        let n_o = idx.stats.file_rows as usize;
        let (mut vf, mut vt, mut size, mut name, mut owner, mut cls) =
            (Vec::with_capacity(n_o), Vec::with_capacity(n_o), Vec::with_capacity(n_o), Vec::with_capacity(n_o), Vec::with_capacity(n_o), Vec::with_capacity(n_o));
        let mut cont = vec![0u64; n_o.div_ceil(64) + 1];
        let (mut dname, mut ddepth, mut dend, mut dlo, mut dhi) = (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
        let (mut dck_off, mut dck) = (vec![0u64], Vec::new());
        let (mut complex_rows, mut complex) = (Vec::new(), Vec::new());
        let mut dself: Vec<u32> = Vec::new();
        // Whether the rows since the last new path are a directory's own versions (the directory is the stack's top).
        let mut own = false;
        // Open directories: (depth, name, slim id or NONE). Those with ids are a prefix (assigned top-down).
        const NONE: u32 = u32::MAX;
        let mut st: Vec<(u8, u32, u32)> = Vec::new();
        let mut assigned = 0usize;
        let mut o = 0usize;
        let close = |e: (u8, u32, u32), o: usize, nd: usize, dend: &mut Vec<u32>, dhi: &mut Vec<u32>| {
            if e.2 != NONE {
                dhi[e.2 as usize] = o as u32;
                dend[e.2 as usize] = (nd - 1) as u32;
            }
        };
        for r in 0..n {
            let f = idx.flags[r];
            if f & CONT == 0 {
                let d = idx.depth[r];
                while st.last().is_some_and(|e| e.0 >= d) {
                    let e = st.pop().unwrap();
                    close(e, o, dname.len(), &mut dend, &mut dhi);
                }
                assigned = assigned.min(st.len());
                own = f & FILE == 0;
                if own {
                    st.push((d, idx.name[r], NONE));
                    continue;
                }
            }
            if f & FILE == 0 {
                continue;
            }
            while assigned < st.len() {
                let id = dname.len();
                if id % CKD == 0 {
                    dck.extend(st[..assigned].iter().map(|e| e.2));
                    dck_off.push(dck.len() as u64);
                }
                let e = &mut st[assigned];
                e.2 = id as u32;
                dname.push(e.1);
                ddepth.push(e.0);
                dend.push(0);
                dlo.push(o as u32);
                dhi.push(0);
                assigned += 1;
            }
            let top = st.last().map_or(NONE, |e| e.2);
            let first_own = own && dself.last() != Some(&top);
            if first_own {
                dself.push(top);
            }
            if f & CONT != 0 && !first_own {
                cont[o >> 6] |= 1 << (o & 63);
            }
            vf.push(idx.vf[r]);
            vt.push(idx.vt[r]);
            size.push(idx.size[r]);
            name.push(idx.name[r]);
            owner.push(idx.owner[r]);
            cls.push(f & !(FILE | CONT));
            if f & COMPLEX != 0 {
                complex_rows.push(o as u32);
                complex.push(idx.complex_of(r).clone());
            }
            o += 1;
        }
        while let Some(e) = st.pop() {
            close(e, o, dname.len(), &mut dend, &mut dhi);
        }
        if o != n_o {
            return err(format!("{o} object rows, the full index counts {n_o}"));
        }
        if o > u32::MAX as usize {
            return err("more than 2^32 object rows: dlo/dhi need u64");
        }
        let nd = dname.len();
        if dck_off.len() != nd.div_ceil(CKD) + 1 {
            return err(format!("{} directory checkpoints for {nd} directories", dck_off.len() - 1));
        }
        let secs = t0.elapsed().as_secs_f64();
        let stats = json!({"rows": o, "dirs": nd, "dirs_with_own_versions": dself.len(), "dirs_dropped": idx.stats.dir_rows as usize - nd, "build_s": secs});
        Ok(SlimIndex {
            scans: idx.scans.clone(),
            owners: idx.owners.clone(),
            dict: Dict {
                lower: idx.dict.lower.to_vec().into(),
                offs: idx.dict.offs.to_vec().into(),
                patch_ids: idx.dict.patch_ids.to_vec().into(),
                patch_offs: idx.dict.patch_offs.to_vec().into(),
                patch_buf: idx.dict.patch_buf.to_vec().into(),
            },
            vf: vf.into(),
            vt: vt.into(),
            size: size.into(),
            name: name.into(),
            owner: owner.into(),
            cls: cls.into(),
            cont: cont.into(),
            complex_rows: complex_rows.into(),
            complex,
            dname: dname.into(),
            ddepth: ddepth.into(),
            dend: dend.into(),
            dlo: dlo.into(),
            dhi: dhi.into(),
            dck_off: dck_off.into(),
            dck: dck.into(),
            dself: dself.into(),
            stats,
            map: None,
        })
    }

    /// Write to `path`, each column group its own section (a page-cache server faults in only what queries read).
    pub fn write(&self, path: &Path) -> Result<u64> {
        let mut w = Writer::default();
        w.add("vf", &self.vf);
        w.add("vt", &self.vt);
        w.add("size", &self.size);
        w.add("cont", &self.cont);
        w.add("ddepth", &self.ddepth);
        w.add("dend", &self.dend);
        w.add("dlo", &self.dlo);
        w.add("dhi", &self.dhi);
        w.add("dck_off", &self.dck_off);
        w.add("dck", &self.dck);
        w.add("dself", &self.dself);
        w.add("name", &self.name);
        w.add("dname", &self.dname);
        w.add("owner", &self.owner);
        w.add("cls", &self.cls);
        w.add("complex_rows", &self.complex_rows);
        add_dict(&mut w, "dict_", &self.dict);
        let cx = ComplexCols::of(&self.complex);
        cx.add(&mut w);
        w.write(path, "slim", json!({"scans": self.scans, "owners": self.owners, "stats": self.stats}))
    }

    pub fn read(p: &Pack) -> Result<SlimIndex> {
        if p.kind != "slim" {
            return err(format!("pack kind {}, not slim", p.kind));
        }
        Ok(SlimIndex {
            scans: serde_json::from_value(p.meta["scans"].clone())?,
            owners: serde_json::from_value(p.meta["owners"].clone())?,
            stats: p.meta["stats"].clone(),
            dict: p.dict("dict_")?,
            vf: p.col("vf")?,
            vt: p.col("vt")?,
            size: p.col("size")?,
            name: p.col("name")?,
            owner: p.col("owner")?,
            cls: p.col("cls")?,
            cont: p.col("cont")?,
            complex_rows: p.col("complex_rows")?,
            complex: p.complex()?,
            dname: p.col("dname")?,
            ddepth: p.col("ddepth")?,
            dend: p.col("dend")?,
            dlo: p.col("dlo")?,
            dhi: p.col("dhi")?,
            dck_off: p.col("dck_off")?,
            dck: p.col("dck")?,
            dself: p.col("dself")?,
            map: Some(p.map.clone()),
        })
    }

    pub fn complex_of(&self, r: usize) -> &Complex {
        let i = self.complex_rows.binary_search(&(r as u32)).expect("a COMPLEX row without its slices");
        &self.complex[i]
    }
}
