//! Storage classes: which of a store's columns hold which class's bytes, and the index's class slots
//! (specs/vm-mode.md §6.1).
//!
//! An object version's bytes split into classes. Each of the store's class columns holds one class's bytes, by class
//! id (gcs: Nearline 2, Coldline 3, Archive 4; hf: private 5, unbilled git 6); class 1 is the rest of `size`. The index
//! keeps a version's class in a **slot** (2 bits of `flags`, `index::CLS_SHIFT`; a complex row's `Complex::cls`): slot
//! 0 is class 1, slots 1–3 the store's class columns by ascending id. `Index::classes` / `SlimIndex::classes` name each
//! slot's class, and a pack records them (`meta.classes`) unless they are `DEFAULT`, which a pack without the key (every
//! pack written before slots were recorded) also means. A request's classes map onto slots through them (`mask`).
//!
//! A `path` sort's class columns are the ones between `wb` and `us`, where every version table of the interval store
//! holds them (`interval_store.CLASS_COLS`). `fsb … -C K=COLUMN,…` names them (`dt-cloud vm-pack build` passes the
//! deployment's `CLASS_COLS`); without it each must be called `c<K>`. In a file, class K's column is its named column,
//! else `c<K>` (a generation written before the deployment renamed it). A class column no class claims, or more than
//! 3 of them, is an error, never a silent drop.

use serde_json::{json, Value};

use crate::util::{err, Result};

/// The index's class slots: class 1 (the rest of `size`) and up to 3 class columns.
pub const SLOTS: usize = 4;
/// Each slot's class when the store's class columns are `c2`..`c4` (and in a pack that doesn't record its slots).
pub const DEFAULT: [u8; SLOTS] = [1, 2, 3, 4];

/// A store's class columns: `(class id, column)`, by ascending id.
pub type ClassCols = Vec<(u8, String)>;

fn list(ks: impl IntoIterator<Item = u8>) -> String {
    ks.into_iter().map(|k| k.to_string()).collect::<Vec<_>>().join(", ")
}

fn named(cs: &[(u8, String)]) -> String {
    cs.iter().map(|(k, c)| format!("{k}={c}")).collect::<Vec<_>>().join(", ")
}

/// Sorted by id, each id ≥ 2 and named once, each column once, at most `SLOTS − 1` of them.
fn checked(mut cs: ClassCols) -> Result<ClassCols> {
    cs.sort();
    for (i, (k, c)) in cs.iter().enumerate() {
        if *k < 2 {
            return err(format!("class {k}={c}: class 1 is the rest of `size`, a class column's id is 2..255"));
        }
        if c.is_empty() {
            return err(format!("class {k}: no column named"));
        }
        if i > 0 && cs[i - 1].0 == *k {
            return err(format!("class {k} named twice ({})", named(&cs)));
        }
        if cs[..i].iter().any(|(_, d)| d == c) {
            return err(format!("column `{c}` named for two classes ({})", named(&cs)));
        }
    }
    if cs.len() > SLOTS - 1 {
        return err(format!(
            "{} class columns ({}): a pack holds at most {} (its slots 1–{}; class 1 is the rest of `size`)",
            cs.len(),
            named(&cs),
            SLOTS - 1,
            SLOTS - 1,
        ));
    }
    Ok(cs)
}

/// `-C`'s value, `K=COLUMN,…` (empty: the store has none).
pub fn parse(spec: &str) -> Result<ClassCols> {
    let mut cs = Vec::new();
    for part in spec.split(',').map(str::trim).filter(|p| !p.is_empty()) {
        let (k, c) = part.split_once('=').ok_or_else(|| format!("-C {spec:?}: `{part}` isn't K=COLUMN"))?;
        let k: u8 = k.trim().parse().map_err(|_| format!("-C {spec:?}: class `{}` isn't 2..255", k.trim()))?;
        cs.push((k, c.trim().to_string()));
    }
    checked(cs)
}

/// A `path` sort's class columns (`names`: its columns, in order): those strictly between `wb` and `us`.
pub fn region(names: &[String]) -> Result<Vec<String>> {
    let at = |c: &str| names.iter().position(|n| n == c).ok_or_else(|| format!("no column `{c}` (a store's class columns are between `wb` and `us`)"));
    let (a, b) = (at("wb")?, at("us")?);
    if b < a {
        return err("`us` before `wb`: can't tell the class columns");
    }
    Ok(names[a + 1..b].to_vec())
}

/// The store's class columns: `named` (`-C`), else read off a file's class columns (`region`), each `c<K>`.
pub fn resolve(named: Option<&[(u8, String)]>, region: &[String]) -> Result<ClassCols> {
    match named {
        Some(cs) => checked(cs.to_vec()),
        None => {
            let id = |c: &String| c.strip_prefix('c').and_then(|k| k.parse::<u8>().ok()).filter(|k| *c == format!("c{k}"));
            let cs = region.iter().map(|c| id(c).map(|k| (k, c.clone())).ok_or_else(|| c.clone())).collect::<std::result::Result<ClassCols, String>>();
            match cs {
                Ok(cs) => checked(cs),
                Err(c) => err(format!("class column `{c}` has no class id: name the store's class columns (`-C K=COLUMN,…`)")),
            }
        }
    }
}

/// One file's column for each of `cs` (in order), among its class columns `region`: the class's named column, else
/// `c<K>`. Every one of `region` must be some class's.
pub fn columns(cs: &[(u8, String)], region: &[String]) -> Result<Vec<String>> {
    let mut used = vec![false; region.len()];
    let mut out = Vec::with_capacity(cs.len());
    for (k, c) in cs {
        let canon = format!("c{k}");
        let i = region
            .iter()
            .position(|x| x == c)
            .or_else(|| region.iter().position(|x| *x == canon))
            .ok_or_else(|| {
                let names = if *c == canon { format!("`{c}`") } else { format!("`{c}` or `{canon}`") };
                format!("no column for class {k} ({names}) among its class columns {region:?}")
            })?;
        if used[i] {
            return err(format!("column `{}` claimed by two classes ({})", region[i], named(cs)));
        }
        used[i] = true;
        out.push(region[i].clone());
    }
    let left: Vec<&String> = region.iter().zip(&used).filter(|(_, u)| !**u).map(|(c, _)| c).collect();
    if !left.is_empty() {
        return err(format!("class columns {left:?} are no class's (classes: {}): name each (`-C K=COLUMN,…`)", if cs.is_empty() { "none".into() } else { named(cs) }));
    }
    Ok(out)
}

/// Each slot's class: 1, then the class columns' ids.
pub fn slots(cs: &[(u8, String)]) -> Vec<u8> {
    std::iter::once(1).chain(cs.iter().map(|c| c.0)).collect()
}

/// The slots as a pack records them: nothing for `DEFAULT`.
pub fn to_meta(slots: &[u8]) -> Option<Value> {
    (slots != DEFAULT.as_slice()).then(|| json!(slots))
}

/// A pack's slots (`meta.classes`; `DEFAULT` when absent).
pub fn from_meta(v: Option<&Value>) -> Result<Vec<u8>> {
    let Some(v) = v else { return Ok(DEFAULT.to_vec()) };
    let s: Vec<u8> = serde_json::from_value(v.clone()).map_err(|e| format!("pack `classes` {v}: {e}"))?;
    if s.is_empty() || s.len() > SLOTS || s[0] != 1 || s.windows(2).any(|w| w[0] >= w[1]) {
        return err(format!("pack `classes` {v}: 1, then up to {} ascending class ids", SLOTS - 1));
    }
    Ok(s)
}

/// A request's classes as slot bits (bit i: slot i). A class the store doesn't hold is an error.
pub fn mask(slots: &[u8], want: &[u8]) -> Result<u8> {
    let mut m = 0u8;
    for &k in want {
        let i = slots.iter().position(|&s| s == k).ok_or_else(|| format!("class {k}: this store's classes are {}", list(slots.iter().copied())))?;
        m |= 1 << i;
    }
    Ok(m)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cols(cs: &[(u8, &str)]) -> ClassCols {
        cs.iter().map(|(k, c)| (*k, c.to_string())).collect()
    }

    fn names(ns: &[&str]) -> Vec<String> {
        ns.iter().map(|n| n.to_string()).collect()
    }

    fn msg<T: std::fmt::Debug>(r: Result<T>) -> String {
        r.unwrap_err().to_string()
    }

    #[test]
    fn parse_spec() {
        assert_eq!(parse("5=private_bytes,6=git_bytes").unwrap(), cols(&[(5, "private_bytes"), (6, "git_bytes")]));
        assert_eq!(parse(" 4=archive_bytes, 2=nearline_bytes ,3=coldline_bytes").unwrap(), cols(&[(2, "nearline_bytes"), (3, "coldline_bytes"), (4, "archive_bytes")]));
        assert_eq!(parse("").unwrap(), cols(&[]));
        assert_eq!(msg(parse("5")), "-C \"5\": `5` isn't K=COLUMN");
        assert_eq!(msg(parse("x=a")), "-C \"x=a\": class `x` isn't 2..255");
        assert_eq!(msg(parse("1=a")), "class 1=a: class 1 is the rest of `size`, a class column's id is 2..255");
        assert_eq!(msg(parse("5=a,5=b")), "class 5 named twice (5=a, 5=b)");
        assert_eq!(msg(parse("5=a,6=a")), "column `a` named for two classes (5=a, 6=a)");
        assert_eq!(
            msg(parse("2=a,3=b,4=c,5=d")),
            "4 class columns (2=a, 3=b, 4=c, 5=d): a pack holds at most 3 (its slots 1–3; class 1 is the rest of `size`)"
        );
    }

    #[test]
    fn class_region() {
        let hf = names(&["depth", "path", "vf", "vt", "kind", "size", "wts", "wb", "private_bytes", "git_bytes", "us", "last_read", "churn_cum"]);
        assert_eq!(region(&hf).unwrap(), names(&["private_bytes", "git_bytes"]));
        assert_eq!(region(&names(&["size", "wb", "us"])).unwrap(), names(&[]));
        assert_eq!(msg(region(&names(&["size", "us"]))), "no column `wb` (a store's class columns are between `wb` and `us`)");
        assert_eq!(msg(region(&names(&["us", "wb"]))), "`us` before `wb`: can't tell the class columns");
    }

    #[test]
    fn resolve_classes() {
        // Without -C: each class column is `c<K>`.
        assert_eq!(resolve(None, &names(&["c2", "c3", "c4"])).unwrap(), cols(&[(2, "c2"), (3, "c3"), (4, "c4")]));
        assert_eq!(resolve(None, &names(&[])).unwrap(), cols(&[]));
        assert_eq!(msg(resolve(None, &names(&["private_bytes"]))), "class column `private_bytes` has no class id: name the store's class columns (`-C K=COLUMN,…`)");
        assert_eq!(msg(resolve(None, &names(&["c02"]))), "class column `c02` has no class id: name the store's class columns (`-C K=COLUMN,…`)");
        // With -C: as named, sorted.
        let hf = cols(&[(6, "git_bytes"), (5, "private_bytes")]);
        assert_eq!(resolve(Some(&hf), &names(&["private_bytes", "git_bytes"])).unwrap(), cols(&[(5, "private_bytes"), (6, "git_bytes")]));
    }

    #[test]
    fn file_columns() {
        let gcs = cols(&[(2, "nearline_bytes"), (3, "coldline_bytes"), (4, "archive_bytes")]);
        assert_eq!(columns(&gcs, &names(&["nearline_bytes", "coldline_bytes", "archive_bytes"])).unwrap(), names(&["nearline_bytes", "coldline_bytes", "archive_bytes"]));
        // A file written before the rename: `c<K>`.
        assert_eq!(columns(&gcs, &names(&["c2", "c3", "c4"])).unwrap(), names(&["c2", "c3", "c4"]));
        let hf = cols(&[(5, "private_bytes"), (6, "git_bytes")]);
        assert_eq!(columns(&hf, &names(&["private_bytes", "git_bytes"])).unwrap(), names(&["private_bytes", "git_bytes"]));
        assert_eq!(msg(columns(&hf, &names(&["private_bytes"]))), "no column for class 6 (`git_bytes` or `c6`) among its class columns [\"private_bytes\"]");
        // cloud's default `-C` on hf's store (a shared box's checkout).
        assert_eq!(
            msg(columns(&cols(&[(2, "c2"), (3, "c3"), (4, "c4")]), &names(&["private_bytes", "git_bytes"]))),
            "no column for class 2 (`c2`) among its class columns [\"private_bytes\", \"git_bytes\"]"
        );
        assert_eq!(
            msg(columns(&hf, &names(&["private_bytes", "git_bytes", "public_bytes"]))),
            "class columns [\"public_bytes\"] are no class's (classes: 5=private_bytes, 6=git_bytes): name each (`-C K=COLUMN,…`)"
        );
        assert_eq!(msg(columns(&[], &names(&["c2", "c3", "c4"]))), "class columns [\"c2\", \"c3\", \"c4\"] are no class's (classes: none): name each (`-C K=COLUMN,…`)");
        assert_eq!(columns(&[], &names(&[])).unwrap(), names(&[]));
    }

    #[test]
    fn slots_and_meta() {
        let gcs = cols(&[(2, "nearline_bytes"), (3, "coldline_bytes"), (4, "archive_bytes")]);
        assert_eq!(slots(&gcs), vec![1, 2, 3, 4]);
        assert_eq!(to_meta(&slots(&gcs)), None);
        let hf = slots(&cols(&[(5, "private_bytes"), (6, "git_bytes")]));
        assert_eq!(hf, vec![1, 5, 6]);
        assert_eq!(to_meta(&hf), Some(json!([1, 5, 6])));
        assert_eq!(slots(&[]), vec![1]);
        assert_eq!(to_meta(&[1]), Some(json!([1])));
        assert_eq!(from_meta(None).unwrap(), vec![1, 2, 3, 4]);
        assert_eq!(from_meta(Some(&json!([1, 5, 6]))).unwrap(), vec![1, 5, 6]);
        assert_eq!(from_meta(Some(&json!([1]))).unwrap(), vec![1]);
        assert_eq!(msg(from_meta(Some(&json!([5, 6])))), "pack `classes` [5,6]: 1, then up to 3 ascending class ids");
    }

    #[test]
    fn request_mask() {
        assert_eq!(mask(&DEFAULT, &[1, 3]).unwrap(), 0b0101);
        assert_eq!(mask(&[1, 5, 6], &[5]).unwrap(), 0b010);
        assert_eq!(mask(&[1, 5, 6], &[6, 1]).unwrap(), 0b101);
        assert_eq!(msg(mask(&[1, 5, 6], &[2])), "class 2: this store's classes are 1, 5, 6");
        assert_eq!(msg(mask(&[1], &[5])), "class 5: this store's classes are 1");
        assert_eq!(msg(mask(&DEFAULT, &[5])), "class 5: this store's classes are 1, 2, 3, 4");
    }
}
