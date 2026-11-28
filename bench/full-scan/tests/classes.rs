//! Class columns from the store (`classes.rs`, specs/vm-mode.md §6.1): an hf-shaped store (classes 5 and 6, named by
//! `-C`), a store with no class columns, and the errors (an unnamed class column, too many), through the loader, the
//! full and slim passes, the pack, and `vmd`.

use std::fs::File;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow_array::{ArrayRef, Int32Array, Int64Array, RecordBatch, StringArray};
use arrow_schema::{DataType, Field, Schema};
use full_scan::classes::{self, ClassCols};
use full_scan::index::Complex;
use full_scan::load::load;
use full_scan::pack::{self, Pack};
use full_scan::query::Query;
use full_scan::scan::{self, Answer};
use full_scan::serve::{now, request_string, sign, Conf, Loaded, Server, TenantState};
use full_scan::slim::SlimIndex;
use full_scan::slim_scan;
use full_scan::store::{self, OPEN};
use parquet::arrow::ArrowWriter;
use serde_json::{json, Value};

/// A fresh scratch dir under cargo's per-target tmp.
fn scratch(name: &str) -> PathBuf {
    let d = Path::new(env!("CARGO_TARGET_TMPDIR")).join("classes").join(name);
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

struct Row {
    depth: i32,
    path: &'static str,
    vf: i64,
    vt: i64,
    kind: &'static str,
    size: i64,
    n_files: i64,
    /// hf's class columns: private (5), unbilled git (6).
    cls: [i64; 2],
}

/// Two scans (`s1`, `s2`). `a/x` is 60 B private + 40 B public, `a/y` all git, `b/z` all public; `b/w` (private)
/// arrives at `s2`. Directories carry their class sums, which the loader doesn't read.
fn rows() -> Vec<Row> {
    let r = |depth, path, vf, vt, kind, size, n_files, cls| Row { depth, path, vf, vt, kind, size, n_files, cls };
    vec![
        r(1, "a", 100, OPEN, "dir", 150, 2, [60, 50]),
        r(1, "b", 100, 200, "dir", 30, 1, [0, 0]),
        r(1, "b", 200, OPEN, "dir", 50, 2, [20, 0]),
        r(2, "a/x", 100, OPEN, "file", 100, 1, [60, 0]),
        r(2, "a/y", 100, OPEN, "file", 50, 1, [0, 50]),
        r(2, "b/w", 200, OPEN, "file", 20, 1, [20, 0]),
        r(2, "b/z", 100, OPEN, "file", 30, 1, [0, 0]),
    ]
}

/// A generation dir: `scans.json` and its base `served/path.parquet`, whose class columns (between `wb` and `us`) are
/// `cls` (each row's `cls` in order; zeros past the second).
fn generation(dir: &Path, cls: &[&str]) -> PathBuf {
    std::fs::create_dir_all(dir.join("served")).unwrap();
    std::fs::write(dir.join("scans.json"), r#"{"scans": [{"id": "s1", "ts": 100}, {"id": "s2", "ts": 200}]}"#).unwrap();
    let rows = rows();
    let i64s = |f: &dyn Fn(&Row) -> i64| -> ArrayRef { Arc::new(Int64Array::from(rows.iter().map(f).collect::<Vec<_>>())) };
    let mut fields = vec![
        Field::new("depth", DataType::Int32, false),
        Field::new("path", DataType::Utf8, false),
        Field::new("vf", DataType::Int64, false),
        Field::new("vt", DataType::Int64, false),
        Field::new("kind", DataType::Utf8, false),
        Field::new("size", DataType::Int64, false),
        Field::new("n_files", DataType::Int64, false),
        Field::new("wb", DataType::Int64, false),
    ];
    let mut cols: Vec<ArrayRef> = vec![
        Arc::new(Int32Array::from(rows.iter().map(|r| r.depth).collect::<Vec<_>>())),
        Arc::new(StringArray::from(rows.iter().map(|r| r.path).collect::<Vec<_>>())),
        i64s(&|r: &Row| r.vf),
        i64s(&|r: &Row| r.vt),
        Arc::new(StringArray::from(rows.iter().map(|r| r.kind).collect::<Vec<_>>())),
        i64s(&|r: &Row| r.size),
        i64s(&|r: &Row| r.n_files),
        i64s(&|_: &Row| 0),
    ];
    for (k, c) in cls.iter().enumerate() {
        fields.push(Field::new(*c, DataType::Int64, false));
        cols.push(i64s(&|r: &Row| r.cls.get(k).copied().unwrap_or(0)));
    }
    fields.push(Field::new("us", DataType::Utf8, false));
    cols.push(Arc::new(StringArray::from(vec![""; rows.len()])));
    let batch = RecordBatch::try_new(Arc::new(Schema::new(fields)), cols).unwrap();
    let mut w = ArrowWriter::try_new(File::create(dir.join("served/path.parquet")).unwrap(), batch.schema(), None).unwrap();
    w.write(&batch).unwrap();
    w.close().unwrap();
    dir.to_path_buf()
}

fn hf_classes() -> ClassCols {
    classes::parse("5=private_bytes,6=git_bytes").unwrap()
}

fn pool() -> rayon::ThreadPool {
    rayon::ThreadPoolBuilder::new().num_threads(2).build().unwrap()
}

/// A view of the root, 2 levels, every nonempty node (`v`'s fields added).
fn query(v: Value) -> Query {
    let mut q = json!({"id": "q", "path": "", "depth": 2});
    for (k, x) in v.as_object().unwrap() {
        q[k.as_str()] = x.clone();
    }
    serde_json::from_value(q).unwrap()
}

type Nodes = Vec<(&'static str, u64, u64, u64, u64)>;

fn answer(total: [u64; 4], nodes: Nodes) -> Answer {
    Answer { id: "q".into(), total, nodes: nodes.into_iter().map(|(p, a, b, c, d)| (p.to_string(), a, b, c, d)).collect(), rest: Vec::new() }
}

/// The view's answer by the full index and by the slim pack read back from `pack` (equal, or both errors, the same).
fn both(idx: &full_scan::index::Index, ix: &SlimIndex, q: Value) -> Result<Answer, String> {
    let (q, p) = (query(q), pool());
    let full = scan::run(idx, &q, &p).map(|(a, _)| a).map_err(|e| e.to_string());
    let slim = slim_scan::run(ix, &q, &p).map(|(a, _, _)| a).map_err(|e| e.to_string());
    assert_eq!(full, slim, "{q:?}: the full and slim passes differ");
    slim
}

/// `gen` loaded (`named` class columns), its slim pack written to `out` and read back.
fn packed(gen: &Path, named: Option<&[(u8, String)]>, out: &Path) -> (full_scan::index::Index, SlimIndex, Pack) {
    let idx = load(&store::open(gen).unwrap(), named).unwrap();
    SlimIndex::from_index(&idx).unwrap().write(out).unwrap();
    let p = Pack::open(out).unwrap();
    let ix = SlimIndex::read(&p).unwrap();
    (idx, ix, p)
}

#[test]
fn hf_store() {
    let d = scratch("hf");
    let gen = generation(&d.join("gen"), &["private_bytes", "git_bytes"]);
    let (idx, ix, p) = packed(&gen, Some(hf_classes().as_slice()), &d.join("hf.fsb"));
    assert_eq!(idx.classes, vec![1, 5, 6]);
    assert_eq!((ix.classes.clone(), p.meta.get("classes").cloned()), (vec![1, 5, 6], Some(json!([1, 5, 6]))));
    // Object rows a/x, a/y, b/w, b/z: a/x split (public + private: a complex row, slot bits clear), a/y slot 2 (git),
    // b/w slot 1 (private), b/z slot 0 (public).
    assert_eq!(ix.cls.to_vec(), vec![16, 2 << 2, 1 << 2, 0]);
    assert_eq!(ix.complex_rows.to_vec(), vec![0]);
    assert_eq!(ix.complex, vec![Complex { owners: vec![(0, 100)], cls_mask: 0b011, cls: [40, 60, 0, 0] }]);
    // The full pack records them too.
    pack::write_index(&idx, &d.join("hf.full.fsb")).unwrap();
    assert_eq!(pack::read_index(&Pack::open(&d.join("hf.full.fsb")).unwrap()).unwrap().classes, vec![1, 5, 6]);

    let at = |classes: Value| both(&idx, &ix, json!({"scan": "s2", "classes": classes}));
    assert_eq!(at(json!([5])), Ok(answer([80, 2, 0, 0], vec![("a", 60, 1, 0, 0), ("a/x", 60, 1, 0, 0), ("b", 20, 1, 0, 0), ("b/w", 20, 1, 0, 0)])));
    assert_eq!(at(json!([6])), Ok(answer([50, 1, 0, 0], vec![("a", 50, 1, 0, 0), ("a/y", 50, 1, 0, 0)])));
    assert_eq!(at(json!([1])), Ok(answer([70, 2, 0, 0], vec![("a", 40, 1, 0, 0), ("a/x", 40, 1, 0, 0), ("b", 30, 1, 0, 0), ("b/z", 30, 1, 0, 0)])));
    assert_eq!(
        at(json!([5, 6])),
        Ok(answer([130, 3, 0, 0], vec![("a", 110, 2, 0, 0), ("a/x", 60, 1, 0, 0), ("a/y", 50, 1, 0, 0), ("b", 20, 1, 0, 0), ("b/w", 20, 1, 0, 0)]))
    );
    assert_eq!(at(json!([2])), Err("class 2: this store's classes are 1, 5, 6".to_string()));
    assert_eq!(
        both(&idx, &ix, json!({"scan": "s2"})),
        Ok(answer([200, 4, 0, 0], vec![("a", 150, 2, 0, 0), ("a/x", 100, 1, 0, 0), ("a/y", 50, 1, 0, 0), ("b", 50, 2, 0, 0), ("b/w", 20, 1, 0, 0), ("b/z", 30, 1, 0, 0)]))
    );
    assert_eq!(
        both(&idx, &ix, json!({"from": "s1", "to": "s2", "classes": [5]})),
        Ok(answer([60, 1, 80, 2], vec![("a", 60, 1, 60, 1), ("a/x", 60, 1, 60, 1), ("b", 0, 0, 20, 1), ("b/w", 0, 0, 20, 1)]))
    );
}

#[test]
fn store_without_class_columns() {
    let d = scratch("none");
    let gen = generation(&d.join("gen"), &[]);
    for (named, out) in [(None, "derived.fsb"), (Some(Vec::new()), "named.fsb")] {
        let (idx, ix, p) = packed(&gen, named.as_deref(), &d.join(out));
        assert_eq!((idx.classes.clone(), ix.classes.clone(), p.meta.get("classes").cloned()), (vec![1], vec![1], Some(json!([1]))));
        assert_eq!((ix.cls.to_vec(), ix.complex.len()), (vec![0, 0, 0, 0], 0));
        let all = answer([200, 4, 0, 0], vec![("a", 150, 2, 0, 0), ("a/x", 100, 1, 0, 0), ("a/y", 50, 1, 0, 0), ("b", 50, 2, 0, 0), ("b/w", 20, 1, 0, 0), ("b/z", 30, 1, 0, 0)]);
        assert_eq!(both(&idx, &ix, json!({"scan": "s2", "classes": [1]})), Ok(all.clone()));
        assert_eq!(both(&idx, &ix, json!({"scan": "s2"})), Ok(all));
        assert_eq!(both(&idx, &ix, json!({"scan": "s2", "classes": [5]})), Err("class 5: this store's classes are 1".to_string()));
    }
}

#[test]
fn class_column_errors() {
    let d = scratch("errors");
    let load_err = |gen: &Path, named: Option<&[(u8, String)]>| load(&store::open(gen).unwrap(), named).err().unwrap().to_string();
    let hf = generation(&d.join("hf"), &["private_bytes", "git_bytes"]);
    let base = hf.join("served/path.parquet").display().to_string();
    assert_eq!(load_err(&hf, None), format!("{base}: class column `private_bytes` has no class id: name the store's class columns (`-C K=COLUMN,…`)"));
    assert_eq!(
        load_err(&hf, Some(classes::parse("5=private_bytes").unwrap().as_slice())),
        format!("{base}: class columns [\"git_bytes\"] are no class's (classes: 5=private_bytes): name each (`-C K=COLUMN,…`)")
    );
    assert_eq!(load_err(&hf, Some(ClassCols::new().as_slice())), format!("{base}: class columns [\"private_bytes\", \"git_bytes\"] are no class's (classes: none): name each (`-C K=COLUMN,…`)"));
    let four = generation(&d.join("four"), &["c2", "c3", "c4", "c5"]);
    assert_eq!(
        load_err(&four, None),
        format!(
            "{}: 4 class columns (2=c2, 3=c3, 4=c4, 5=c5): a pack holds at most 3 (its slots 1–3; class 1 is the rest of `size`)",
            four.join("served/path.parquet").display()
        )
    );
}

#[test]
fn vmd_maps_classes_through_the_pack() {
    let d = scratch("vmd");
    let hf = generation(&d.join("hf"), &["private_bytes", "git_bytes"]);
    let none = generation(&d.join("none"), &[]);
    // gcs's class columns (`c2`..`c4`), which the loader names by itself; not the site's fixture, whose class columns
    // are the checkout's deployment's.
    let gcs = generation(&d.join("gcs"), &["c2", "c3", "c4"]);
    let packs: Vec<(&str, PathBuf, Option<ClassCols>)> = vec![("hf", hf, Some(hf_classes())), ("none", none, None), ("g1", gcs, None)];
    let mut tenants = Vec::new();
    for (name, gen, named) in &packs {
        let out = d.join(format!("{name}.fsb"));
        packed(gen, named.as_deref(), &out);
        tenants.push(json!({"name": name, "pack": out, "key_env": "VMD_KEY_CLASSES_TEST", "prefault": false}));
    }
    let conf = d.join("vmd.json");
    std::fs::write(&conf, json!({"threads": 2, "tenants": tenants}).to_string()).unwrap();
    std::env::set_var("VMD_KEY_CLASSES_TEST", "k");
    let srv = Server::new(conf.clone()).unwrap();
    for t in Conf::read(&conf).unwrap().tenants {
        let l = Loaded::open(&t, 2).unwrap();
        srv.tenants.write().unwrap().insert(t.name.clone(), TenantState { current: Some(Arc::new(l)), ..Default::default() });
    }
    let call = |tenant: &str, method: &str, path: &str, body: Value| -> (u16, Value) {
        let body = if body.is_null() { Vec::new() } else { body.to_string().into_bytes() };
        let ts = now().to_string();
        let sig = sign(b"k", &request_string(method, path, tenant, &ts, &body));
        let (r, _) = srv.handle(method, path, Some(tenant), Some(ts.as_str()), Some(sig.as_str()), &body);
        (r.status, r.body)
    };
    let status = |tenant: &str| {
        let (s, b) = call(tenant, "GET", "/v1/status", Value::Null);
        (s, b["current"]["classes"].clone())
    };
    // The recorded slots; a pack that records none (a store whose class columns are `c2`..`c4`) is the default.
    assert_eq!([status("hf"), status("none"), status("g1")], [(200, json!([1, 5, 6])), (200, json!([1])), (200, json!([1, 2, 3, 4]))]);

    let view = |tenant: &str, classes: Value| {
        let (s, b) = call(tenant, "POST", "/v1/view", json!({"pins": {}, "scan": "s2", "classes": classes, "cell": {"depth": 2}}));
        if s == 200 {
            (s, json!({"total": b["total"], "nodes": b["nodes"], "tree": b["tree"]}))
        } else {
            (s, b)
        }
    };
    let leaf = |name: &str, kind: &str, bytes: u64, objects: u64, children: Vec<Value>| {
        let mut n = json!({"name": name, "kind": kind, "bytes": bytes, "objects": objects});
        if !children.is_empty() {
            n["children"] = json!(children);
        }
        n
    };
    assert_eq!(
        view("hf", json!([5])),
        (
            200,
            json!({
                "total": {"bytes": 80, "objects": 2},
                "nodes": 4,
                "tree": leaf("", "dir", 80, 2, vec![
                    leaf("a", "dir", 60, 1, vec![leaf("x", "file", 60, 1, vec![])]),
                    leaf("b", "dir", 20, 1, vec![leaf("w", "file", 20, 1, vec![])]),
                ]),
            })
        )
    );
    assert_eq!(
        view("hf", json!([6])),
        (200, json!({"total": {"bytes": 50, "objects": 1}, "nodes": 2, "tree": leaf("", "dir", 50, 1, vec![leaf("a", "dir", 50, 1, vec![leaf("y", "file", 50, 1, vec![])])])}))
    );
    assert_eq!(view("hf", json!([2])), (400, json!({"error": "bad-request", "message": "class 2: this store's classes are 1, 5, 6"})));
    assert_eq!(view("none", json!([5])), (400, json!({"error": "bad-request", "message": "class 5: this store's classes are 1"})));
    assert_eq!(
        view("none", json!([1])),
        (
            200,
            json!({
                "total": {"bytes": 200, "objects": 4},
                "nodes": 6,
                "tree": leaf("", "dir", 200, 4, vec![
                    leaf("a", "dir", 150, 2, vec![leaf("x", "file", 100, 1, vec![]), leaf("y", "file", 50, 1, vec![])]),
                    leaf("b", "dir", 50, 2, vec![leaf("z", "file", 30, 1, vec![]), leaf("w", "file", 20, 1, vec![])]),
                ]),
            })
        )
    );
}
