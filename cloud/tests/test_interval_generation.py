"""The objects' native version (GCS `generation`) in the interval store (specs/lean-scans.md §3.1): from raw listings
through `path-index -G` into a generation's object versions — build, append, carries, compaction — out again by
`export`, checked by `verify`; added to a generation built without it by `backfill-generation`; and read back by a
sweep's `as_of` hold once the scan's listing is gone.

The world: one bucket, seven scans. The lister records `generation` from the third scan on (GCS's, since 2026-10-07).
Objects: one never changed; one deleted before the first generation; one rewritten (a new generation, created later);
two rewritten before the first generation, one of which lives on; one whose metadata changed (`updated`, the version's
`mtime`, moved; same generation); objects appearing later."""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import interval_backfill as ib
from dt_cloud import interval_compact as ic
from dt_cloud import interval_export as ie
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import interval_verify as iv
from dt_cloud import static_names as sn
from dt_cloud.append_runner import latest_key
from dt_cloud.blobstore import Store
from dt_cloud.staged_plan import parse_plan
from dt_cloud.sweep_manifest import HoldSource, build_manifests
from dt_cloud.viz import write_path_index

B = "b"
SCANS = ["2026-10-05", "2026-10-06", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"]
#: The first scan whose listing has `generation`.
GEN_FROM = 2
T0 = 1_760_000_000
#: Per object, per scan: `(size, created, updated, generation)` (seconds past T0), or None while absent.
OBJ = {
    "d/keep.bin": [(10, 100, 100, 11)] * 7,
    "d/old.bin": [(10, 100, 100, 31)] * 2 + [None] * 5,
    "d/rew.bin": [(10, 100, 100, 21)] * 3 + [(20, 300, 300, 22)] * 4,
    "d/rw2.bin": [(10, 50, 50, 50)] + [(10, 60, 60, 51)] * 6,
    "d/meta.bin": [(10, 100, 100, 61)] * 4 + [(10, 100, 400, 61)] * 3,
    "d/new.bin": [None] * 4 + [(10, 500, 500, 41)] * 3,
    "d/sub/deep.bin": [(10, 100, 100, 71)] * 7,
    "d/new2.bin": [None] * 6 + [(10, 600, 600, 81)],
}
#: Every object version: `(path, first scan, scan it closed at or None, generation)`.
VERSIONS = [
    ("b/d/keep.bin", 0, None, 11),
    ("b/d/meta.bin", 0, 4, 61), ("b/d/meta.bin", 4, None, 61),
    ("b/d/new.bin", 4, None, 41),
    ("b/d/new2.bin", 6, None, 81),
    ("b/d/old.bin", 0, 2, None),
    ("b/d/rew.bin", 0, 3, 21), ("b/d/rew.bin", 3, None, 22),
    ("b/d/rw2.bin", 0, 1, None), ("b/d/rw2.bin", 1, None, 51),
    ("b/d/sub/deep.bin", 0, None, 71),
]
ON, OFF = "index/{id}/path-index.parquet", "index-off/{id}/path-index.parquet"
LISTING = "listing/{id}/*/*.parquet"
RG = 4


def _through(n: int) -> list[tuple]:
    """`VERSIONS` as a generation through scan `n` holds them."""
    return [(p, a, b if b is not None and b <= n else None, g) for p, a, b, g in VERSIONS if a <= n]


def _ts(sec: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(T0 + sec, dt.timezone.utc)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("ivg")
    for j, d in enumerate(SCANS):
        rows = sorted((name, *v[j]) for name, v in OBJ.items() if v[j] is not None)
        cols = {
            "bucket": pa.array([B] * len(rows)), "name": pa.array([r[0] for r in rows]),
            "size_bytes": pa.array([r[1] for r in rows], pa.int64()),
            "created": pa.array([_ts(r[2]) for r in rows], pa.timestamp("us", tz="UTC")),
            "storage_class_id": pa.array([1] * len(rows), pa.int64()),
            "updated": pa.array([_ts(r[3]) for r in rows], pa.timestamp("us", tz="UTC")),
        }
        if j >= GEN_FROM:
            cols["generation"] = pa.array([r[4] for r in rows], pa.int64())
        shard = root / "listing" / d / B / "shard-00.parquet"
        shard.parent.mkdir(parents=True)
        pq.write_table(pa.table(cols), shard)
        for layout, gen in ((ON, True), (OFF, False)):
            write_path_index((str(shard),), root / "snap" / layout.split("/")[0] / d, d, path_index=root / layout.replace("{id}", d), generation=gen)
    return root


def _build(world: Path, store: Path, work: Path, layout: str, through: int, gen: str = "g") -> dict:
    args = ["-g", gen, "-L", layout, "-s", str(world), "-o", str(store), "-w", str(work), "-k", "2", "-r", str(RG), "-p", "2", "-M", "1GB",
            "-t", SCANS[through]]
    res = CliRunner().invoke(il.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _add(world: Path, store: Path, work: Path, layout: str, through: int, gen: str = "g") -> dict:
    args = ["-g", gen, "-L", layout, "-s", str(world), "-o", str(store), "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB", "-t", SCANS[through]]
    res = CliRunner().invoke(il.add_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _newest_runs(gen_dir: Path) -> list[Path]:
    ms = sorted((gen_dir / "manifests").glob("*.json")) if (gen_dir / "manifests").is_dir() else []
    if not ms:
        return []
    key = latest_key([p.relative_to(gen_dir).as_posix() for p in ms])
    return [gen_dir / r["key"] for r in json.loads((gen_dir / key).read_text())["runs"]]


def _versions(gen_dir: Path) -> list[tuple]:
    """The generation's object versions (base `pvl/` and the newest manifest's runs' deltas, one row per version, the
    smallest `vt`): `(path, first scan, closing scan or None, generation)`."""
    files = [str(p) for p in sorted((gen_dir / "pvl").glob("r*.parquet"))]
    files += [str(p) for r in _newest_runs(gen_dir) for p in sorted((r / "pvl").glob("r*.parquet"))]
    idx = {sn.scan_epoch(d): j for j, d in enumerate(SCANS)}
    con = duckdb.connect()
    union = " UNION ALL ".join(f"SELECT depth, path, vf, vt, kind, {ist.GENERATION} FROM read_parquet('{f}')" for f in files)
    rows = con.execute(f"""SELECT path, vf, vt, {ist.GENERATION} FROM (SELECT * FROM ({union})
        QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1) WHERE kind = 'file' ORDER BY path, vf""").fetchall()
    return [(p, idx[vf], idx.get(vt), g) for p, vf, vt, g in rows]


def _columns(path: Path) -> list[str]:
    return pq.read_schema(path).names


def _parquets(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*.parquet"))}


@pytest.fixture(scope="module")
def on(world, tmp_path_factory):
    """A store over sorts that carry `generation`: the base built over the first three scans, three more added."""
    tmp = tmp_path_factory.mktemp("ivg-on")
    store = tmp / "store"
    _build(world, store, tmp / "w-build", ON, 2)
    _add(world, store, tmp / "w-add", ON, 5)
    return {"tmp": tmp, "store": store, "gen": store / ist.PREFIX / "g"}


@pytest.fixture(scope="module")
def off(world, tmp_path_factory):
    """A store over sorts without it (today's every deployment): base over four scans, two added."""
    tmp = tmp_path_factory.mktemp("ivg-off")
    store = tmp / "store"
    _build(world, store, tmp / "w-build", OFF, 3)
    _add(world, store, tmp / "w-add", OFF, 5)
    return {"tmp": tmp, "store": store, "gen": store / ist.PREFIX / "g"}


def test_versions_carry_generation_through_build_append_and_carries(world, on, tmp_path):
    gen = on["gen"]
    assert json.loads((gen / "scans.json").read_text())["generation"] is True
    assert _versions(gen) == _through(5)
    # A merged run (the carry of the first two appends) holds them as the level-0 runs did.
    assert [r.name for r in _newest_runs(gen)] == [f"{SCANS[3]}_{SCANS[4]}", SCANS[5]]
    # A rebuild over every scan versions them the same.
    full = tmp_path / "full"
    _build(world, full, tmp_path / "w", ON, 5)
    assert _versions(full / ist.PREFIX / "g") == _through(5)
    # Served: the `path` sort carries it, last; `bysize` and the slice sorts never do.
    for served in [gen / "served", *(r / "served" for r in _newest_runs(gen))]:
        assert _columns(served / "path.parquet") == [*ist.PVL_SCHEMA.names, "generation"]
        assert _columns(served / "bysize.parquet") == ist.PVL_SCHEMA.names
    assert _columns(gen / "pv" / "r0000.parquet") == [*ist.PV_SCHEMA.names, "generation"]


def test_compaction_keeps_generation(on, tmp_path):
    out = tmp_path / "g.c001"
    shutil.copytree(on["gen"], tmp_path / "g")
    ic.compact_gen(tmp_path / "g", out, tmp_path / "w", gen="g", new_gen="g.c001", level=None, rg_rows=RG, threads=2, mem="1GB")
    assert _versions(out) == _through(5)
    assert json.loads((out / "scans.json").read_text())["generation"] is True
    assert _columns(out / "served" / "path.parquet") == [*ist.PVL_SCHEMA.names, "generation"]
    assert _columns(out / "pv" / "r0000.parquet") == [*ist.PV_SCHEMA.names, "generation"]


def test_compaction_refuses_a_generation_borrowing_its_ranges(on, tmp_path):
    shutil.copytree(on["gen"], tmp_path / "g")
    (tmp_path / "g" / "ranges.json").unlink()
    with pytest.raises(SystemExit) as e:
        ic.compact_gen(tmp_path / "g", tmp_path / "g.c001", tmp_path / "w", gen="g", new_gen="g.c001", level=None, rg_rows=RG, threads=2, mem="1GB")
    assert (str(e.value), (tmp_path / "g.c001").exists()) == (
        "g: no ranges.json: a staged build's generation borrowing its ranges and base from another (its `ranges_gen`) can't be "
        "compacted in place; backfill-generation -R makes a self-contained one", False)


def _export(store: Path, scan: str, dest: Path, gen: str | None = None) -> pa.Table:
    args = ["-o", str(store), "-w", str(dest.parent / f"w-{dest.name}"), "-t", "path", *(["-g", gen] if gen else []), scan, str(dest)]
    res = CliRunner().invoke(ie.export_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return pq.read_table(dest / "path-index.parquet")


def _objects(t: pa.Table) -> list[tuple]:
    return sorted((r["path"], r["generation"]) for r in t.to_pylist() if r["kind"] == "file")


def test_export_reproduces_generation(world, on, tmp_path):
    for j in range(GEN_FROM, 6):
        d = SCANS[j]
        got = _export(on["store"], d, tmp_path / d)
        assert got.column_names[-1] == "generation"
        assert _objects(got) == _objects(pq.read_table(world / ON.replace("{id}", d))), d
    # A scan before the lister recorded it: its versions' generation (listed later within the same version).
    assert _objects(_export(on["store"], SCANS[0], tmp_path / SCANS[0])) == [
        ("b/d/keep.bin", 11), ("b/d/meta.bin", 61), ("b/d/old.bin", None), ("b/d/rew.bin", 21), ("b/d/rw2.bin", None), ("b/d/sub/deep.bin", 71)]


def _verify(store: Path, gen: str, world: Path, j: int, tmp: Path) -> dict:
    s = {"id": SCANS[j], "src": ON.replace("{id}", SCANS[j]), "ts": sn.scan_epoch(SCANS[j]), "version": 2}
    return iv.verify_scans(Store(str(store)), gen, str(world), [s], tmp)[SCANS[j]]


def test_verify_compares_generation(world, on, off, tmp_path):
    """Each sampled scan's objects with a generation against the store's live version: in the marker, and required for
    `pass`; a store or scan without the column has no such check."""
    m = _verify(on["store"], "g", world, 5, tmp_path / "on")
    # Objects with a generation at the scan: keep, meta, new, rew, rw2, deep.
    assert (m["pass"], m["generation"]) == (True, {"objects": 6, "equal": 6})
    m = _verify(off["store"], "g", world, 5, tmp_path / "off")
    assert (m["pass"], sorted(m)) == (True, ["diff_eq", "diffs", "gen", "pass", "scan", "view_eq", "views"])


def test_off_has_no_generation_anywhere(world, off, tmp_path):
    """Without the opt-in no file carries the column, not even NULL: sorts, range files, deltas, served, exports."""
    gen = off["gen"]
    assert "generation" not in json.loads((gen / "scans.json").read_text())
    with_col = [k for k in _parquets(off["store"]) if "generation" in _columns(off["store"] / k)]
    assert with_col == []
    assert "generation" not in _columns(world / OFF.replace("{id}", SCANS[3]))
    assert "generation" not in _export(off["store"], SCANS[4], tmp_path / "x").column_names


def _backfill(store: Path, work: Path, world: Path, to: str = "g2", *extra: str) -> dict:
    args = ["-o", str(store), "-O", to, "-s", str(world), "-l", LISTING, "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB", *extra]
    res = CliRunner().invoke(ib.backfill_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


@pytest.fixture(scope="module")
def backfilled(world, off, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ivg-bf")
    store = tmp / "store"
    shutil.copytree(off["store"], store)
    before = _parquets(store / ist.PREFIX / "g")
    out = _backfill(store, tmp / "w", world)
    return {"tmp": tmp, "store": store, "out": out, "before": before}


def test_backfill_fills_versions_live_since_the_first_generation(world, backfilled):
    store, out = backfilled["store"], backfilled["out"]
    g2 = store / ist.PREFIX / "g2"
    # Exactly the versions live at or after the first listing with generations; earlier ones stay NULL.
    assert _versions(g2) == _through(5)
    assert json.loads((g2 / "scans.json").read_text())["generation"] is True
    assert json.loads((g2 / ib.BACKFILL).read_text()) == {
        "from": "g", "manifest": f"manifests/{SCANS[5]}.m001.json", "runs": [f"deltas/{SCANS[4]}_{SCANS[5]}"], "scans": 6, "listings": SCANS[GEN_FROM:6], "listed": 7, "filled": 8}
    assert (out["listings"], out["skipped"], out["filled"]) == (SCANS[GEN_FROM:6], SCANS[:GEN_FROM], 8)
    # The source and the pointer are untouched.
    assert _parquets(store / ist.PREFIX / "g") == backfilled["before"]
    assert json.loads((store / ic.CURRENT).read_text()) == {"gen": "g"}
    # Served as a build over generation-bearing sorts serves it (its versions are that build's: `_through(5)` above).
    assert _columns(g2 / "served" / "path.parquet") == [*ist.PVL_SCHEMA.names, "generation"]


def test_a_parallel_backfill_writes_the_serial_ones_bytes(world, off, backfilled, tmp_path):
    """`-j 3`: both key ranges at once, each filled (`filler`'s temp table) on its own cursor, then the served sorts three
    at a time: the serial backfill's files and counts."""
    store = tmp_path / "store"
    shutil.copytree(off["store"], store)
    out = _backfill(store, tmp_path / "w", world, "g2", "-j", "3")
    g2, ref = store / ist.PREFIX / "g2", backfilled["store"] / ist.PREFIX / "g2"
    assert (out["filled"], out["rows"]) == (backfilled["out"]["filled"], backfilled["out"]["rows"])
    assert ((g2 / ib.BACKFILL).read_text(), (g2 / "served" / "stats.json").read_text()) == ((ref / ib.BACKFILL).read_text(), (ref / "served" / "stats.json").read_text())
    assert sorted(_parquets(g2)) == sorted(_parquets(ref))
    assert _parquets(g2) == _parquets(ref)


def test_backfill_borrows_a_staged_generations_ranges(world, off, backfilled, tmp_path):
    """A staged build's generation holds only its scans, runs and served sorts; its key ranges and base range files live
    in its ranges generation. `-R` mirrors them in, and the new generation is the same as when the source held them."""
    store = tmp_path / "store"
    shutil.copytree(off["store"], store)
    g, gr = store / ist.PREFIX / "g", store / ist.PREFIX / "gr"
    gr.mkdir()
    for p in [g / "ranges.json", *(g / t for t in ist.SUB_SCHEMA if (g / t).exists())]:
        shutil.move(p, gr / p.name)
    res = CliRunner().invoke(ib.backfill_cmd, ["-o", str(store), "-g", "g", "-O", "g2", "-s", str(world), "-l", LISTING, "-w", str(tmp_path / "w0")])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{store}/{ist.PREFIX}/g/ranges.json: none; a staged build's generation names its ranges with -R")
    _backfill(store, tmp_path / "w", world, "g2", "-g", "g", "-R", "gr")
    g2, ref = store / ist.PREFIX / "g2", backfilled["store"] / ist.PREFIX / "g2"
    assert (_versions(g2), _parquets(g2), (g2 / "ranges.json").read_text()) == (_versions(ref), _parquets(ref), (ref / "ranges.json").read_text())


def test_backfill_refuses_an_existing_generation_or_one_that_has_it(world, backfilled, tmp_path):
    args = ["-o", str(backfilled["store"]), "-O", "g2", "-s", str(world), "-l", LISTING, "-w", str(tmp_path)]
    res = CliRunner().invoke(ib.backfill_cmd, args)
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{backfilled['store']}/{ist.PREFIX}/g2/ exists: a generation is never rewritten")
    res = CliRunner().invoke(ib.backfill_cmd, [*args[:2], "-g", "g2", "-O", "g3", *args[4:]])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{backfilled['store']}/{ist.PREFIX}/g2: already carries `generation`")


def test_backfill_refuses_a_template_matching_files_with_and_without_generation(world, off, tmp_path):
    """A template that also matches a non-listing parquet sorting first (gcs's `listing/{id}/dir-cache/age-days.parquet`
    under `listing/{id}/*/*.parquet`) is refused, naming it, instead of skipping every scan."""
    root = tmp_path / "world"
    shutil.copytree(world, root)
    stray = root / "listing" / SCANS[GEN_FROM] / "dir-cache" / "age-days.parquet"
    stray.parent.mkdir(parents=True)
    pq.write_table(pa.table({"age_days": [1]}), stray)
    store = tmp_path / "store"
    shutil.copytree(off["store"], store)
    args = ["-o", str(store), "-O", "g2", "-s", str(root), "-l", LISTING, "-w", str(tmp_path / "w")]
    res = CliRunner().invoke(ib.backfill_cmd, args)
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (
        1, f"{SCANS[GEN_FROM]}: {LISTING!r} matches 2 files, 1 without a `generation` column ({stray.relative_to(root).as_posix()}): narrow it to the listing shards")


def test_gen_index_reads_every_shard_of_a_bucket(tmp_path):
    """A scan's listing in several shards per bucket (every real lister's): each shard's objects are indexed, not only
    the first's."""
    root = tmp_path / "root"
    for i, names in enumerate((["d/a.bin", "d/b.bin"], ["d/c.bin"])):
        shard = root / "listing" / SCANS[0] / B / f"shard-{i:02d}.parquet"
        shard.parent.mkdir(parents=True, exist_ok=True)
        n = len(names)
        pq.write_table(pa.table({"bucket": [B] * n, "name": names, "size_bytes": [1] * n, "created": pa.array([None] * n, pa.timestamp("us", "UTC")),
                                 "storage_class_id": [1] * n, "generation": pa.array([10 + i * 10 + j for j in range(n)], pa.int64())}), shard)
    con = duckdb.connect()
    work = tmp_path / "w"
    work.mkdir()
    out = ib.gen_index(con, Store(str(root)), LISTING, [{"id": SCANS[0], "ts": T0}], work)
    assert (out, con.execute(f"SELECT depth, path, generation FROM read_parquet('{work / ib.GEN_INDEX}')").fetchall()) == (
        {"scans": [SCANS[0]], "skipped": [], "rows": 3},
        [(3, "b/d/a.bin", 10), (3, "b/d/b.bin", 11), (3, "b/d/c.bin", 20)])


def test_verify_generation_checks_the_store_against_each_listing(world, backfilled, tmp_path):
    """`verify-generation`: per scan of the backfilled generation, its listed generations against the store's at that scan,
    recorded on the scan's marker (from the source generation; `pass` then needs them all equal). A scan whose listing
    has none is skipped, one without a marker gets none, and a listing the store disagrees with fails its marker."""
    store = tmp_path / "store"
    shutil.copytree(backfilled["store"], store)
    root = tmp_path / "world"
    shutil.copytree(world, root)
    shard = root / "listing" / SCANS[3] / B / "shard-00.parquet"
    t = pq.read_table(shard)
    gens = [99 if n == "d/keep.bin" else g for n, g in zip(t["name"].to_pylist(), t["generation"].to_pylist())]
    pq.write_table(t.set_column(t.schema.get_field_index("generation"), "generation", pa.array(gens, pa.int64())), shard)
    verify = store / ist.PREFIX / "g" / "verify"
    verify.mkdir(parents=True, exist_ok=True)
    for d in (*SCANS[:4], SCANS[5]):
        (verify / f"{d}.json").write_text(json.dumps({"scan": d, "gen": "g", "pass": True, "views": 9, "view_eq": 9, "diffs": 1, "diff_eq": 1}))
    args = ["-o", str(store), "-g", "g2", "-s", str(root), "-l", LISTING, "-w", str(tmp_path / "w")]
    res = CliRunner().invoke(ib.verify_generation_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    g = lambda n, eq: {"objects": n, "equal": eq, "missing": 0, "against": "listing"}  # noqa: E731
    assert json.loads(res.stdout) == {"gen": "g2", "scans": {
        SCANS[0]: {"status": "no-generation", "generation": None},
        SCANS[1]: {"status": "no-generation", "generation": None},
        SCANS[2]: {"status": "pass", "generation": g(5, 5)},
        SCANS[3]: {"status": "fail", "generation": g(5, 4)},
        SCANS[4]: {"status": "no-marker", "generation": g(6, 6)},
        SCANS[5]: {"status": "pass", "generation": g(6, 6)},
    }}
    written = {p.stem: json.loads(p.read_text()) for p in sorted((store / ist.PREFIX / "g2" / "verify").glob("*.json"))}
    assert written == {
        d: {"scan": d, "gen": "g2", "views_gen": "g", "pass": ok, "views": 9, "view_eq": 9, "diffs": 1, "diff_eq": 1, "generation": gen}
        for d, ok, gen in ((SCANS[2], True, g(5, 5)), (SCANS[3], False, g(5, 4)), (SCANS[5], True, g(6, 6)))
    }


def test_gen_index_since_skips_unread_and_a_url_root_copies_only_scans_it_reads(world, tmp_path, monkeypatch):
    """`since`: earlier scans are skipped without a listing call. Over a URL root, a scan's shards are judged by their
    footers (ranged reads), so a scan without generations copies nothing; one with them is copied, every shard."""
    from dt_cloud import blobstore

    root = Store("memory://ivg-since")
    for p in sorted((world / "listing").rglob("*.parquet")):
        root.write_bytes(p.relative_to(world).as_posix(), p.read_bytes())
    gets, listed = [], []
    real_get, real_keys = blobstore.Store.get, blobstore.Store.keys
    monkeypatch.setattr(blobstore.Store, "get", lambda self, k, dst: (gets.append(k), real_get(self, k, dst))[1])
    monkeypatch.setattr(blobstore.Store, "keys", lambda self, prefix: (listed.append(prefix), real_keys(self, prefix))[1])
    scans = [{"id": d, "ts": T0 + i} for i, d in enumerate(SCANS[:4])]
    con = duckdb.connect()
    work = tmp_path / "w"
    work.mkdir()
    out = ib.gen_index(con, root, LISTING, scans, work, since=SCANS[1])
    assert (out["scans"], out["skipped"]) == (SCANS[GEN_FROM:4], SCANS[:GEN_FROM])
    assert sorted({k.split("/")[1] for k in listed}) == SCANS[1:4]
    assert sorted({k.split("/")[1] for k in gets}) == SCANS[GEN_FROM:4]


def test_the_watchdog_exits_when_nothing_beats(monkeypatch):
    """A stalled read (nothing calls `beat` for `stall_s`) ends the process with code 3; a beating one doesn't."""
    import os
    import time

    exits = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    with ib.Watchdog(0.2, "t") as w:
        w.stop.wait(0)  # started
        time.sleep(0.1)
        w.beat()
        time.sleep(0.1)
        w.beat()
        assert exits == []
        deadline = time.monotonic() + 5
        while not exits and time.monotonic() < deadline:
            time.sleep(0.05)
    assert exits[:1] == [3]


def test_a_backfilled_generation_takes_appends_and_verifies(world, backfilled, tmp_path):
    store = tmp_path / "store"
    shutil.copytree(backfilled["store"], store)
    _add(world, store, tmp_path / "w", ON, 6, gen="g2")
    g2 = store / ist.PREFIX / "g2"
    assert _versions(g2) == _through(6)
    m = _verify(store, "g2", world, 5, tmp_path / "verify")
    assert (m["pass"], m["generation"]) == (True, {"objects": 6, "equal": 6})


def _rename_classes(d: Path) -> None:
    """Every version file under `d` with its class columns renamed (`x2`…), as a generation written before a rename."""
    for p in sorted(d.rglob("*.parquet")):
        if p.name.endswith(".groups.parquet"):
            continue
        t = pq.read_table(p)
        if "wb" not in t.column_names:
            continue
        pq.write_table(t.rename_columns([f"x{c[1:]}" if c in ist.CLS else c for c in t.column_names]), p)


def test_backfill_reads_renamed_class_columns_by_position(world, off, backfilled, tmp_path):
    store = tmp_path / "store"
    shutil.copytree(off["store"], store)
    _rename_classes(store / ist.PREFIX / "g")
    _backfill(store, tmp_path / "w", world)
    assert _parquets(store / ist.PREFIX / "g2") == _parquets(backfilled["store"] / ist.PREFIX / "g2")


# ── A sweep's `as_of` hold from the store ──────────────────────────────────


PLAN = {"plan_id": 9, "name": "Staged", "sweep": [f"gs://{B}/d/"], "as_of": {f"gs://{B}/d/": SCANS[2]}}


def _manifest(world: Path, out: Path, source: HoldSource) -> tuple[dict, list[tuple]]:
    sp = parse_plan(PLAN)
    summary = build_manifests(str(world), SCANS[5], dict(sp.sweep), str(out), workers=1, window=1, as_of=sp.as_of, hold_source=source)
    return summary, [(r["name"], r["generation"]) for r in pq.read_table(out / "manifest" / f"{B}.parquet").to_pylist()]


def test_object_identities_at_a_scan(on, off):
    t = ie.object_identities(str(on["store"]), SCANS[2], B, ["d/"])
    assert t.to_pylist() == [
        {"name": "d/keep.bin", "generation": 11, "created": _ts(100)}, {"name": "d/meta.bin", "generation": 61, "created": _ts(100)},
        {"name": "d/rew.bin", "generation": 21, "created": _ts(100)}, {"name": "d/rw2.bin", "generation": 51, "created": _ts(60)},
        {"name": "d/sub/deep.bin", "generation": 71, "created": _ts(100)}]
    assert ie.object_identities(str(on["store"]), SCANS[2], B, [], ["d/rew.bin"]).to_pylist() == [
        {"name": "d/rew.bin", "generation": 21, "created": _ts(100)}]
    # Without the column: no generation, the created second all the same.
    assert [(r["name"], r["generation"]) for r in ie.object_identities(str(off["store"]), SCANS[2], B, ["d/"]).to_pylist()] == [
        ("d/keep.bin", None), ("d/meta.bin", None), ("d/rew.bin", None), ("d/rw2.bin", None), ("d/sub/deep.bin", None)]


def test_a_held_item_resolves_from_the_store_as_from_the_listing(world, on, off, tmp_path):
    """The as_of scan's listing and the store vouch for the same objects: kept the unchanged (a metadata update keeps
    its generation), refused the replaced (`rew`: a new generation) and the new. With the listing gone, `auto` reads
    the store; a store without generations falls back to `created` and refuses the same."""
    want_rows = [("d/keep.bin", 11), ("d/meta.bin", 61), ("d/rw2.bin", 51), ("d/sub/deep.bin", 71)]
    want = {B: {"objects": 6, "dirs": 2, "eligible": {"bytes": 40, "objects": 4},
                "skipped_after_as_of": {"bytes": 30, "objects": 2}}}
    listing = _manifest(world, tmp_path / "listing", HoldSource("listing"))
    assert listing == (want, want_rows)
    assert _manifest(world, tmp_path / "store", HoldSource("store", str(on["store"]))) == listing
    assert _manifest(world, tmp_path / "store-off", HoldSource("store", str(off["store"]))) == listing
    gone = tmp_path / "world"
    shutil.copytree(world / "listing", gone / "listing")
    shutil.rmtree(gone / "listing" / SCANS[2])
    assert _manifest(gone, tmp_path / "auto", HoldSource("auto", str(on["store"]))) == listing
    with pytest.raises(SystemExit) as e:
        _manifest(gone, tmp_path / "none", HoldSource("auto"))
    assert str(e.value) == f"no listing shards for {B} under {gone}/listing/{SCANS[2]}/"


def test_sweep_manifest_cli_reads_the_store(world, on, tmp_path, monkeypatch):
    from dt_cloud import cli

    monkeypatch.setattr(cli, "_hard_exit", lambda: None)  # the command ends the process (`os._exit`) after its writes
    main = cli.main
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(PLAN))
    args = ["sweep", "manifest", "-d", SCANS[5], "-p", str(plan), "-r", str(world)]
    res = CliRunner().invoke(main, [*args, "-H", "store", "-o", str(tmp_path / "x")])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, "-H store needs the interval store (-i)")
    res = CliRunner().invoke(main, [*args, "-H", "store", "-i", str(on["store"]), "-o", str(tmp_path / "out")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    rows = [(r["name"], r["generation"]) for r in pq.read_table(tmp_path / "out" / "manifest" / f"{B}.parquet").to_pylist()]
    assert rows == [("d/keep.bin", 11), ("d/meta.bin", 61), ("d/rw2.bin", 51), ("d/sub/deep.bin", 71)]


def test_listing_files_lists_only_the_scans_dir(tmp_path, monkeypatch):
    """A scan's shards are found by listing that scan's dir, not the parent of every scan's (`listing/`): per scan, the
    whole tree's walk took ~2 min on gcs's mount and ran once per scan in the store."""
    for scan in ("2026-10-05", "2026-10-06"):
        for key in (f"listing/{scan}/marin-a/shard-00-0000.parquet", f"listing/{scan}/marin-b/shard-00-0000.parquet",
                    f"listing/{scan}/dir-cache/age-days.parquet"):
            (tmp_path / key).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / key).write_bytes(b"x")
    root = Store(str(tmp_path))
    asked = []
    real = root.keys
    monkeypatch.setattr(root, "keys", lambda prefix: asked.append(prefix) or real(prefix))
    files = ib.listing_files(root, "listing/{id}/*/shard-*.parquet", "2026-10-05", tmp_path / "w")
    assert asked == ["listing/2026-10-05/"]
    assert [Path(f).relative_to(tmp_path).as_posix() for f in files] == [
        "listing/2026-10-05/marin-a/shard-00-0000.parquet",
        "listing/2026-10-05/marin-b/shard-00-0000.parquet",
    ]


def test_store_versions_one_sort_is_the_windowed_table(tmp_path):
    """A single served sort (a base alone: one row per version) is copied as is, skipping the window that merges a version's
    rows across a base and its runs: the same table, without sorting gcs's 1.2B rows in memory."""
    rows = {"depth": [2, 2, 3], "path": ["b/d", "b/d", "b/d/x"], "vf": [1, 5, 1], "vt": [5, 2**31, 2**31],
            "kind": ["dir", "dir", "file"], ist.GENERATION: [None, None, 7]}
    f = tmp_path / "path.parquet"
    pq.write_table(pa.table(rows), f)
    con = duckdb.connect()
    ib.store_versions(con, [str(f)])
    windowed = con.execute(f"""SELECT * FROM read_parquet('{f}')
        QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1 ORDER BY ALL""").fetchall()
    assert con.execute(f"SELECT * FROM {ib.VERSIONS} ORDER BY ALL").fetchall() == windowed == [
        (2, "b/d", 1, 5, "dir", None), (2, "b/d", 5, 2**31, "dir", None), (3, "b/d/x", 1, 2**31, "file", 7)]


def test_store_versions_base_and_runs_is_the_windowed_union(tmp_path):
    """A base plus runs: a run's close record (a smaller `vt`) wins over the base's open row of the same version, a run's
    new version is added, a version the runs never touch is the base's; the same table as one window over the union,
    without windowing the base's rows the runs don't share."""
    O = 2**31
    def write(name: str, rows: list[tuple]) -> str:
        cols = ["depth", "path", "vf", "vt", "kind", ist.GENERATION]
        f = tmp_path / f"{name}.parquet"
        pq.write_table(pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)}), f)
        return str(f)
    base = write("base", [(2, "b/a", 1, O, "file", 3), (2, "b/c", 1, O, "file", 4), (2, "b/d", 1, 5, "file", 5)])
    run1 = write("run1", [(2, "b/a", 1, 7, "file", 3), (2, "b/a", 7, O, "file", 8)])
    run2 = write("run2", [(2, "b/a", 7, 9, "file", 8), (2, "b/e", 9, O, "file", 9)])
    con = duckdb.connect()
    ib.store_versions(con, [base, run1, run2])
    windowed = con.execute(f"""SELECT * FROM read_parquet([{', '.join(repr(f) for f in (base, run1, run2))}])
        QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1 ORDER BY ALL""").fetchall()
    assert con.execute(f"SELECT * FROM {ib.VERSIONS} ORDER BY ALL").fetchall() == windowed == [
        (2, "b/a", 1, 7, "file", 3), (2, "b/a", 7, 9, "file", 8), (2, "b/c", 1, O, "file", 4), (2, "b/d", 1, 5, "file", 5),
        (2, "b/e", 9, O, "file", 9)]
