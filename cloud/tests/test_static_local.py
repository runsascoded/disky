"""`dt_cloud.static_local`: the single-machine `static-names build` writes the generation the staged path's stages write
(byte for byte: coalesced versions, suffix shards, sidecar, catalog; the drill as `static_drill.build_drill` over the
staged roots; the name index and anchors as `static_anchors`' base builders over the staged versions and shards), and
`static-names add` appends scans as runs that read, under the combine rule, as the generation rebuilt over every scan
(suffix rows, catalog, first hits and answers on every date; anchored and path-term views equal brute force)."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import duckdb
import fsspec
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from dt_cloud import static_anchors as an
from dt_cloud import static_append as sa
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn

from test_static_anchors import _prefixes, _start_rows, _views
from test_static_append import TERMS, _catalog, _combine, _reader, _sx
from test_static_catalog import _gen
from test_static_drill import _drill
from test_static_names import _brute_answer, _oracle, fixture, scan_ids  # noqa: F401

V = 3
K = 2  # scans appended after the base
R, KEEP, FLOOR = 3, 2, 2
#: Anchored keys over the fixture's names (`^q`, `q$`, `^q$`; present and absent).
AKEYS = [an.term_key(t, m) for t, m in [
    (".txt", "end"), ("txt", "end"), ("parquet", "end"), (".crc", "end"), ("418", "end"), ("5418y", "end"), ("zzz", "end"),
    ("48", "start"), ("48.p", "start"), ("gof", "start"), ("ago", "start"), ("x54", "start"), ("é54", "start"), ("nk", "start"), ("zz", "start"),
    ("48.parquet", "exact"), ("ab", "exact"), ("sh", "exact"), ("gof", "exact"), ("e5418", "exact"), ("pio", "exact"), ("nope", "exact"),
]]
#: Path terms (plain: the lowercase path holds the term, its parent doesn't).
PATH_TERMS = ["b1/gof", "gof/gof", "1/g", "d/ab", "e5418/x54", "f/48.parquet", "b2/f", "/48", "zz/q"]


def _layout(scans: dict) -> str:
    s = scans["scans"][0]
    return s["src"].replace(s["id"], "{id}")


def _build(root: Path, scans: dict, store: Path | str, work: Path, gen: str = "g", through: str | None = None, extra: tuple[str, ...] = ()):
    args = ["-g", gen, "-L", _layout(scans), "-s", str(root), "-o", str(store), "-x", "off", "-w", str(work), "-k", "2", "-n", "60",
            "-V", str(V), "-R", str(R), "-K", str(KEEP), "-f", str(FLOOR), "-p", "2", "-M", "1GB", *(["-t", through] if through else []), *extra]
    res = CliRunner().invoke(sl.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def _add(root: Path, scans: dict, store: Path, work: Path, gen: str = "g", through: str | None = None, grace_s: int | None = None):
    res = CliRunner().invoke(sl.add_cmd, ["-g", gen, "-L", _layout(scans), "-s", str(root), "-o", str(store), "-w", str(work), "-p", "2", "-M", "1GB",
                                          *(["-t", through] if through else []), *(["-G", str(grace_s)] if grace_s else [])],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


@contextmanager
def _small_groups(rows: int = 5):
    """Row groups of `rows` rows in the suffix shards and name index (`static_names` / `static_append`), and the
    anchors' record of it (`static_anchors.SX_RG`, `meta.json`'s `rg`: the reader reads a key's roots up to R + 4·rg
    rows before its rollups)."""
    rg = (sn.SX_RG, sa.SX_RG, an.SX_RG)
    sn.SX_RG = sa.SX_RG = an.SX_RG = rows
    try:
        yield
    finally:
        sn.SX_RG, sa.SX_RG, an.SX_RG = rg


def _files(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def built(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("local")
    with _small_groups():  # several row groups per shard, as `_gen`
        summary = _build(root, scans, tmp / "store", tmp / "work")
        staged = _gen(root, scans, tmp / "staged", V)
        # the reference anchored tier: the name index of the staged versions, then `anchors/` over its shards
        ref = tmp / "ref-anchors"
        cint = sorted(str(f) for f in (staged["build"] / "cintervals").glob("*.parquet"))
        an.write_names(staged["con"], an.files_sql(cint, an.VERSION_COLS), ref / an.NAMES)
        an.build_tier_local(staged["con"], an.Tier(staged["out"]).files("end"), an.Tier(ref).files("exact"), ref / an.ANCHORS, R, KEEP,
                            scan_ids(scans))
    return {"root": root, "scans": scans, "merged": merged, "summary": summary, "gen": tmp / "store" / sn.PREFIX / "g", "staged": staged,
            "ref_anchors": ref, "tmp": tmp}


def _world(fx: dict) -> dict:
    """`test_static_anchors._views`' inputs: every path of the fixture, and the versions (ms) from the oracle."""
    oracle = _oracle(fx["merged"])
    return {"paths": sorted({path for depth, path, *_ in oracle if depth >= 1}),
            "versions": [(path, usr, vf * 1000, vt * 1000, size, n) for depth, path, usr, vf, vt, _k, size, n, *_ in oracle if depth >= 1]}


def _ms(t) -> int:
    return int(t.timestamp()) * 1000


def _path_hits(rows: list[dict], term: str, P: str, D_ms: int) -> dict[str, list[int]]:
    """A path term's view at `P` on scan `D_ms` (per child of P, Σ size, n_files of the live match roots): `rows` are
    versions `(path, usr, vf, vt, size, n_files)`; a root holds the term in its lowercase path, its parent doesn't."""
    def parent(path: str) -> str:
        return path.rsplit("/", 1)[0] if "/" in path else ""

    hits = [r for r in rows if (P == "" or r["path"].startswith(P + "/")) and term in r["path"].lower() and term not in parent(r["path"].lower())]
    return an.child_sums(hits, P, D_ms)


def _name_rows(tiers: list[Path], term: str) -> list[dict]:
    """The site's read of a path term: the name-index range `/<tail>…` (tail: the term after its last `/`) over the
    tiers, combined (equal `(s, path, usr, vf)`: the smallest `vt`)."""
    tail = "/" + term.rsplit("/", 1)[-1]
    rows: dict[tuple, dict] = {}
    for t in tiers:
        for f in sorted((t / an.NAMES / "sx").glob("*.parquet")):
            for r in pq.read_table(f).to_pylist():
                if r["s"].startswith(tail):
                    k = (r["s"], r["path"], r["usr"], r["vf"])
                    if k not in rows or r["vt"] < rows[k]["vt"]:
                        rows[k] = r
    return [{**r, "vf": _ms(r["vf"]), "vt": _ms(r["vt"])} for r in rows.values()]


def _path_term_views(world: dict, tiers: list[Path], dates: list[str]) -> tuple[list, list]:
    versions = [{"path": p, "usr": u, "vf": vf, "vt": vt, "size": s, "n_files": n} for p, u, vf, vt, s, n in world["versions"]]
    dirs = [""] + sorted({p.rsplit("/", 1)[0] for p in world["paths"] if "/" in p} | set(world["paths"]))
    got, want = [], []
    for term in PATH_TERMS:
        rows = _name_rows(tiers, term)
        for P in dirs:
            for d in dates:
                D = sn.scan_epoch(d) * 1000
                got.append((term, P, d, _path_hits(rows, term, P, D)))
                want.append((term, P, d, _path_hits(versions, term, P, D)))
    return got, want


def test_build_writes_the_staged_generation(built):
    gen, staged = built["gen"], built["staged"]
    assert json.loads((gen / "ranges.json").read_text()) == staged["ranges"]
    for sub, src in (("cintervals", staged["build"]), ("chist", staged["build"]), ("sx", staged["out"]), ("sidecar", staged["out"])):
        assert _files(gen / sub) == _files(src / sub), sub
    assert pq.read_table(gen / "sidecar.parquet").equals(staged["side"])
    assert json.loads((gen / "shards.json").read_text())["shards"] == sn.plan_shards(sorted((staged["build"] / "chist").glob("*.parquet")), 60, 2)["shards"]
    for f in ("cells.parquet", "index.parquet"):
        assert (gen / "catalog" / f).read_bytes() == (staged["final"] / f).read_bytes(), f


def test_build_scans_json(built):
    root, scans = built["root"], built["scans"]
    doc = json.loads((built["gen"] / "scans.json").read_text())
    assert doc == {"bucket": str(root), "layouts": [_layout(scans)],
                   "scans": [{**s, "size": (root / s["src"]).stat().st_size} for s in scans["scans"]]}


def test_build_drill_is_the_reference_drill(built):
    """The drill equals `build_drill` over the staged generation's roots (the test reference: aliases by exact root set,
    where the build aliases by root-set digest); `meta.json` adds the measurement floor."""
    gen, staged, tmp = built["gen"], built["staged"], built["tmp"]
    ref = tmp / "ref" / "drill"
    meas = _drill(staged["con"], staged, ref, R, KEEP, FLOOR)
    got, want = _files(gen / "drill"), _files(ref)
    # the members' digests (the classes' source for a compaction: specs/static-incremental-compaction.md step 3) are the
    # build's own; one row per member, the aliases' members
    digests = got.pop("digests-long.parquet")
    assert sorted(got) == sorted(want)
    assert {k: v for k, v in got.items() if k != "meta.json"} == {k: v for k, v in want.items() if k != "meta.json"}
    assert sorted(pq.read_table(pa.BufferReader(digests), columns=["q"]).column("q").to_pylist()) == sorted(
        pq.read_table(gen / "drill" / "aliases.parquet", columns=["q"]).column("q").to_pylist())
    assert json.loads(got["meta.json"]) == {**json.loads(want["meta.json"]), "floor": FLOOR}
    for sub, kind in (("dirs", "long"), ("short/dirs", "short")):
        got_meas = pq.read_table(gen / "roots-measure" / sub / "s0000.parquet").to_pylist()
        assert got_meas == sorted(pq.read_table(meas[kind]).to_pylist(), key=lambda r: (r["q"], r["k"], r["dir"])), kind
    # The fixture's preconditions for that comparison to cover the roots' sort (`measure_ranges`' per range,
    # `write_kind`'s over the table): rows tying on `(q, path, usr)` (a path's versions) and a unique full key.
    for kind in ("long", "short"):
        keys = [tuple(r.values()) for r in pq.read_table(gen / "drill" / kind / "roots", columns=["q", "path", "usr", "vf"]).to_pylist()]
        assert len(set(keys)) == len(keys), kind
        assert len({k[:3] for k in keys}) < len(keys), kind


def test_build_chunked_drill_is_the_unchunked_one(built, tmp_path):
    """`-c 1`: each member loop chunk one suffix row group, each short piece about one root, each measurement / rollup range
    one literal: the same `drill/` and `roots-measure/`, byte for byte, as the default's one chunk per shard, one piece per
    coalesced-versions file and one range per kind."""
    root, scans, gen = built["root"], built["scans"], built["gen"]
    with _small_groups():
        summary = _build(root, scans, tmp_path / "store", tmp_path / "work", extra=("-c", "1"))
    chunked = tmp_path / "store" / sn.PREFIX / "g"
    for sub in ("drill", "roots-measure"):
        assert _files(chunked / sub) == _files(gen / sub), sub
    shard = dict(zip(*pq.read_table(gen / "catalog" / "members.parquet", columns=["q", "shard"]).to_pydict().values()))
    held = sorted(set(shard.values()))
    long_qs = set(pq.read_table(gen / "drill" / "long" / "roots", columns=["q"]).column("q").to_pylist())
    short_qs = set(pq.read_table(gen / "drill" / "short" / "roots", columns=["q"]).column("q").to_pylist())
    files = len(list((gen / "cintervals").glob("*.parquet")))
    assert len(held) > 1 and len(long_qs) > 1
    assert {k: built["summary"]["drill"][k] for k in ("chunks", "pieces", "groups")} == {"chunks": len(held), "pieces": files,
                                                                                       "groups": {"long": 1, "short": 1}}
    assert {k: summary["drill"][k] for k in ("chunks", "groups")} == {
        "chunks": sum(pq.ParquetFile(gen / "sx" / f"s{i:04d}.parquet").metadata.num_row_groups for i in held),
        "groups": {"long": len(long_qs), "short": len(short_qs)}}
    assert summary["drill"]["pieces"] > files


@pytest.mark.parametrize("heavy_bytes", [None, "1", "median"])
def test_parallel_tail_is_the_serial_one(built, tmp_path, monkeypatch, heavy_bytes):
    """The drill's tail over process pools — each `q` range measured in a process of its own (`MEASURE_THREADS` 1 at
    `-p 4`: 4 at a time; or every range heavy, `HEAVY_THREADS` 2: 2 at a time, heaviest first; or the ranges above the
    median heavy, in a pool beside the light ones' pool), the anchors' rollups and
    starts-with catalog shard by shard (`ANCHOR_THREADS` 1; `write_kind`'s file pool:
    `test_writing_sorted_chunks_writes_the_same_bytes`) — writes `drill/`, `roots-measure/`, `names/` and `anchors/`
    byte for byte as the build at `-p 2` (one range, file and kind at a time on one database)."""
    from dt_cloud import static_drill as sd

    if heavy_bytes:
        monkeypatch.setenv(sl.HEAVY_BYTES_ENV, "1")
        monkeypatch.setattr(sl, "HEAVY_THREADS", 2)
        monkeypatch.setattr(sl, "HEAVY_MEM", 1)
        monkeypatch.setattr(sl, "LIGHT_MEM", 0)
    split: list[tuple[int, int]] = []
    if heavy_bytes == "median":
        spill_files = sl.spill_files

        def median_split(work, kind):
            # the heavy bound at the median range's spilled bytes: heavy and light ranges both, measured beside each other
            got = spill_files(work, kind)
            sizes = sorted(sum(f.stat().st_size for f in fs) for fs in got.values())
            if sizes:
                bound = sizes[len(sizes) // 2]
                monkeypatch.setenv(sl.HEAVY_BYTES_ENV, str(bound))
                split.append((sum(x >= bound for x in sizes), sum(x < bound for x in sizes)))
            return got
        monkeypatch.setattr(sl, "spill_files", median_split)
    monkeypatch.setattr(sl, "MEASURE_THREADS", 1)
    monkeypatch.setattr(an, "ANCHOR_THREADS", 1)
    monkeypatch.setattr(sd, "WRITE_JOBS", 3)
    root, scans, gen = built["root"], built["scans"], built["gen"]
    with _small_groups():
        summary = _build(root, scans, tmp_path / "store", tmp_path / "work", extra=("-c", "1", "-p", "4"))
    par = tmp_path / "store" / sn.PREFIX / "g"
    for sub in ("drill", "roots-measure", an.NAMES, an.ANCHORS):
        assert _files(par / sub) == _files(gen / sub), sub
    assert len(list((gen / "sx").glob("*.parquet"))) > 1  # the anchors' `end` rollups really ran per shard
    assert min(summary["drill"]["groups"].values()) > 1  # and the measurement per range
    if heavy_bytes == "median":
        assert split and all(h >= 1 and lt >= 1 for h, lt in split), split  # both pools really ran


@pytest.mark.parametrize("target_rows", [1, 7])
def test_parallel_names_are_the_streamed_ones(built, tmp_path, target_rows):
    """`write_names` at `jobs` 3 (shards planned from the prefixes' rows, spilled by shard, written in processes): the
    stream's shards, sidecars, `sidecar.parquet` and `shards.json`, byte for byte, with shards cut at `target_rows`."""
    cint = sorted(str(f) for f in (built["gen"] / "cintervals").glob("*.parquet"))
    con = sn.connect(4, "1GB", tmp_path / "duckdb")
    got = {}
    with _small_groups():
        for jobs in (1, 3):
            doc = an.write_names(con, an.files_sql(cint, an.VERSION_COLS), tmp_path / f"names{jobs}", target_rows, jobs=jobs)
            got[jobs] = (doc, _files(tmp_path / f"names{jobs}"))
    con.close()
    assert got[3] == got[1]
    assert got[1][0]["shards"] > 1
    assert sorted(p.name for p in tmp_path.joinpath("duckdb").iterdir() if p.name.startswith("names-shards-")) == []


def test_build_anchors_are_the_staged_anchors(built):
    """`names/` and `anchors/` (with `anchors/start/`): the base builders' over the staged generation's versions and suffix
    shards, byte for byte."""
    gen, ref = built["gen"], built["ref_anchors"]
    got = {f"{sub}/{k}": v for sub in (an.NAMES, an.ANCHORS) for k, v in _files(gen / sub).items()}
    want = {f"{sub}/{k}": v for sub in (an.NAMES, an.ANCHORS) for k, v in _files(ref / sub).items()}
    assert sorted(got) == sorted(want)
    assert got == want
    assert json.loads((gen / an.ANCHORS / "meta.json").read_text())["scans"] == scan_ids(built["scans"])


def test_build_anchored_views_equal_brute_force(appended):
    """The anchored reader over a base (`appended`'s over every scan) answers `^q`, `q$`, `^q$` at every path on every
    scan as brute force: whole ranges (light), and with the whole-range bounds at 0 rows through the rollups, scoped
    roots and the starts-with catalog."""
    w, gen, ids = _world(appended), appended["full"], scan_ids(appended["scans"])
    for kw in ({}, {"max_rows": 0, "start_max_rows": 0}):
        got, want, sources = _views(w, [gen], ids, keys=AKEYS, **kw)
        assert got == want
        assert sources["light" if not kw else "rollup"] > 0
    keys = _prefixes(w["paths"])
    got, want, _ = _views(w, [gen], ids, keys=keys, max_rows=0, start_max_rows=0)
    assert got == want
    # the catalog's members at the fleet root are exactly the prefixes with more than R rows
    reader = an.AnchoredReader([an.reader_tier(fsspec.filesystem("file"), str(gen))], max_rows=0, start_max_rows=0)
    rows = _start_rows([gen])
    members = sorted(k for k in keys if reader.view(k, "", ids)["source"] == "catalog")
    assert members == sorted(k for k in keys if rows.get(k, 0) > R)
    assert len(members) > 3


def test_build_path_terms_equal_brute_force(built):
    """A path term's views read from the name index (its tail's range, folded by the path test) equal brute force from
    the versions, at every directory on every scan."""
    got, want = _path_term_views(_world(built), [built["gen"]], scan_ids(built["scans"]))
    assert got == want
    assert sum(1 for x in want if x[3]) > 0


def test_build_without_anchors(fixture, tmp_path):  # noqa: F811
    """`-A`: no name index, no `anchors/` (the readers decline anchored terms, as before)."""
    root, scans, _ = fixture
    _build(root, scans, tmp_path / "store", tmp_path / "work", extra=("-A",))
    gen = tmp_path / "store" / sn.PREFIX / "g"
    assert ((gen / an.NAMES).exists(), (gen / an.ANCHORS).exists()) == (False, False)


def test_build_refuses_an_existing_generation(built, tmp_path):
    res = CliRunner().invoke(sl.build_cmd, ["-g", "g", "-L", _layout(built["scans"]), "-s", str(built["root"]), "-o", str(built["tmp"] / "store"),
                                            "-x", "off", "-w", str(tmp_path)])
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (1, f"{built['tmp'] / 'store'}/{sn.PREFIX}/g/ exists: a generation is never rewritten")


@pytest.fixture(scope="module")
def appended(fixture, tmp_path_factory):  # noqa: F811
    root, scans, merged = fixture
    tmp = tmp_path_factory.mktemp("local-add")
    n = len(scans["scans"])
    with _small_groups(1):  # one-row groups: the reader's roots bound is R + 4 rows, and the fixture's keys reach the rollups
        _build(root, scans, tmp / "store", tmp / "work", through=scans["scans"][n - K - 1]["id"])
        base_files = _files(tmp / "store" / sn.PREFIX / "g")
        out = _add(root, scans, tmp / "store", tmp / "work-add")
        _build(root, scans, tmp / "full", tmp / "work-full")
    return {"root": root, "scans": scans, "merged": merged, "out": out, "gen": tmp / "store" / sn.PREFIX / "g", "full": tmp / "full" / sn.PREFIX / "g",
            "base_files": base_files, "tmp": tmp}


def test_add_publishes_each_scan_then_the_carry(appended):
    ids = scan_ids(appended["scans"])
    a, b = ids[-2], ids[-1]
    gen = appended["gen"]
    assert sorted(p.name for p in (gen / "manifests").iterdir()) == [f"{a}.json", f"{b}.json", f"{b}.m001.json"]
    newest = json.loads((gen / "manifests" / f"{b}.m001.json").read_text())
    assert [(r["key"], r["level"], r["scans"]) for r in newest["runs"]] == [(sa.run_key(a, b), 1, [a, b])]
    assert newest["scans"] == ids
    assert [x["scan"] for x in appended["out"]["added"]] == [a, b]
    # the base's files are never rewritten
    assert {k: v for k, v in _files(gen).items() if k in appended["base_files"]} == appended["base_files"]


def test_add_reads_as_the_rebuild(appended):
    """Base ⊕ the level-0 runs, and base ⊕ the merged run: the rebuild's suffix rows, catalog and answers."""
    ids = scan_ids(appended["scans"])
    gen, full = appended["gen"], appended["full"]
    level0 = [gen / sa.run_key(d, d) for d in ids[-K:]]
    merged = gen / sa.run_key(ids[-K], ids[-1])
    for tiers in (level0, [merged]):
        assert _combine(_sx([gen, *tiers])) == _sx([full])
        out = appended["tmp"] / "check" / tiers[0].name
        sa.merge_catalogs([gen / "catalog", *(t / "catalog" for t in tiers)], out)
        for f in ("cells.parquet", "index.parquet"):
            assert (out / f).read_bytes() == (full / "catalog" / f).read_bytes(), f
        tiered = sa.TieredReader([_reader(gen, pq.read_table(gen / "sidecar.parquet")), *(_reader(t, pq.read_table(t / "sidecar.parquet")) for t in tiers)])
        rebuilt = sa.TieredReader([_reader(full, pq.read_table(full / "sidecar.parquet"))])
        oracle = _oracle(appended["merged"])
        for term in TERMS:
            if len(term) >= 3:
                assert tiered.hits(term) == rebuilt.hits(term), term
                assert tiered.answer(term, ids)["answers"] == {d: _brute_answer(oracle, term, d) for d in ids}, term
        tiered_cat = sa.TieredCatalog([_catalog(gen / "catalog"), *(_catalog(t / "catalog") for t in tiers)])
        for term in TERMS:
            assert tiered_cat.answer(term, ids) == _catalog(full / "catalog").answer(term, ids), term


def test_add_drills_each_run(appended):
    ids = scan_ids(appended["scans"])
    gen = appended["gen"]
    for d in ids[-K:]:
        meta = json.loads((gen / sa.run_key(d, d) / "drill" / "meta.json").read_text())
        assert (meta["first"], meta["last"], meta["scans"]) == (d, d, [d])
    assert json.loads((gen / sa.run_key(ids[-K], ids[-1]) / "drill" / "meta.json").read_text())["scans"] == ids[-K:]


def _stored_rows(con, tier: Path, kind: str, alias: dict[str, str]) -> dict[tuple[str, str], int]:
    """A drill tier's stored root rows per `(q, dir)` (each ancestor directory of each root), `q` the base's canonical."""
    files = [str(f) for f in sorted((tier / kind / "roots").glob("*.parquet"))]
    rows = con.execute(f"SELECT q, path FROM read_parquet({files})").fetchall() if files else []
    out: dict[tuple[str, str], int] = {}
    for qq, path in rows:
        segs = path.split("/")
        for k in range(1, len(segs)):
            key = (alias.get(qq, qq), "/".join(segs[:k]))
            out[key] = out.get(key, 0) + 1
    return out


def _new_rows(con, base: Path, run: Path, kind: str, alias: dict[str, str]) -> dict[tuple[str, str], int]:
    """A run's stored root rows new to the stack per `(q, dir)` (each ancestor directory of each root): those whose
    `(q, path, usr, vf)` the base doesn't hold (a close record restates one it does), `q` the base's canonical."""
    def keys(tier: Path) -> list[tuple]:
        files = [str(f) for f in sorted((tier / kind / "roots").glob("*.parquet"))]
        return [(alias.get(r[0], r[0]), *r[1:]) for r in con.execute(f"SELECT q, path, usr, vf FROM read_parquet({files})").fetchall()] if files else []
    held = set(keys(base))
    out: dict[tuple[str, str], int] = {}
    for qq, path, usr, vf in keys(run):
        if (qq, path, usr, vf) in held:
            continue
        segs = path.split("/")
        for k in range(1, len(segs)):
            key = (qq, "/".join(segs[:k]))
            out[key] = out.get(key, 0) + 1
    return out


def _full_headers(con, tier: Path, kind: str, alias: dict[str, str]) -> set[tuple[str, str]]:
    files = [str(f) for f in sorted((tier / kind / "rollups").glob("*.parquet"))]
    return {(alias.get(qq, qq), d) for qq, d in con.execute(f"SELECT q, dir FROM read_parquet({files}) WHERE kind = 0").fetchall()} if files else set()


def test_first_add_agrees_with_the_base_on_heaviness(appended):
    """The first run over a fresh base makes heavy exactly the `(q, dir)`s its roots new to the stack push over R: not
    heavy in the base, base rows + the run's new rows > R (a close record restates a base root: not counted). Nothing heavy
    in the base turns heavy again: the base's rollups, `roots-measure/` and the run's `dcount` agree on what is already
    heavy."""
    import duckdb

    ids = scan_ids(appended["scans"])
    gen = appended["gen"]
    base, run = gen / "drill", gen / sa.run_key(ids[-K], ids[-K]) / "drill"
    alias = {r["q"]: r["canonical"] for r in pq.read_table(base / "aliases.parquet").to_pylist()}
    con = duckdb.connect()
    crossed = 0
    for kind in ("long", "short"):
        a = alias if kind == "long" else {}
        heavy = _full_headers(con, base, kind, a)
        before, today = _stored_rows(con, base, kind, a), _new_rows(con, base, run, kind, a)
        want = {k for k, n in today.items() if k not in heavy and before.get(k, 0) + n > R}
        assert _full_headers(con, run, kind, a) - heavy == want, kind
        assert {k for k in want if before.get(k, 0) > R} == set(), kind
        crossed += len(want)
    assert crossed > 0


@pytest.mark.parametrize("stack", ["level0", "merged"])
def test_add_anchored_views_equal_brute_force(appended, stack):
    """Base ⊕ the level-0 runs (each with `names/`, `anchors/` and `anchors/start/`), and base ⊕ the carry's merged run:
    `^q`, `q$`, `^q$` (whole ranges, then bounds 0: rollups, scoped roots, the starts-with catalog) and path terms
    answer every scan as brute force."""
    ids = scan_ids(appended["scans"])
    gen = appended["gen"]
    tiers = [gen / sa.run_key(d, d) for d in ids[-K:]] if stack == "level0" else [gen / sa.run_key(ids[-K], ids[-1])]
    markers = [sorted(p.relative_to(t).as_posix() for p in (t / an.ANCHORS).rglob("meta.json")) for t in tiers]
    assert markers == [["anchors/meta.json", "anchors/start/meta.json"]] * len(tiers)
    # each run tier's census: a level-0 run's is its shards' rows per key, exactly; a merged run's sums its inputs', so it
    # bounds its rows from above (the merge drops rows a later run closed)
    con = duckdb.connect()
    for t in tiers:
        for kind in an.KINDS:
            census = con.execute(f"SELECT s, n FROM read_parquet({an.q(str(an.Tier(t).census(kind)))})").fetchall()
            rows = dict(con.execute(f"SELECT s, count(*) FROM ({an.files_sql(an.Tier(t).files(kind), 's, depth')}) WHERE depth >= 1 GROUP BY s").fetchall())
            assert census
            if stack == "level0":
                assert census == sorted(rows.items())
            else:
                assert [(s, n >= rows.get(s, 0)) for s, n in census] == [(s, True) for s, _ in census]
                assert sorted(rows) == sorted(s for s, n in census if s in rows)
    w = _world(appended)
    for kw in ({}, {"max_rows": 0, "start_max_rows": 0}):
        got, want, sources = _views(w, [gen, *tiers], ids, keys=AKEYS, **kw)
        assert got == want
        assert sources["light" if not kw else "rollup"] > 0
    got, want, sources = _views(w, [gen, *tiers], ids, keys=_prefixes(w["paths"]), max_rows=0, start_max_rows=0)
    assert got == want
    assert sources["catalog"] > 0
    got, want = _path_term_views(w, [gen, *tiers], ids)
    assert got == want


def test_add_to_a_generation_without_anchors(fixture, tmp_path):  # noqa: F811
    """A generation built with `-A`: `add` appends its runs without `names/` or `anchors/` (nothing to stack them on)."""
    root, scans, _ = fixture
    ids = scan_ids(scans)
    _build(root, scans, tmp_path / "store", tmp_path / "work", through=ids[-2], extra=("-A",))
    out = _add(root, scans, tmp_path / "store", tmp_path / "work-add")
    assert [(x["scan"], x["anchors"]) for x in out["added"]] == [(ids[-1], None)]
    gen = tmp_path / "store" / sn.PREFIX / "g"
    assert sorted(p.relative_to(gen).as_posix() for p in gen.rglob("*") if p.name in (an.NAMES, an.ANCHORS)) == []


def test_add_with_nothing_pending_is_a_nop(appended):
    before = _files(appended["gen"])
    out = _add(appended["root"], appended["scans"], appended["tmp"] / "store", appended["tmp"] / "work-nop")
    a, b = scan_ids(appended["scans"])[-2:]
    # the carry's revision is the newest manifest; the ones it and `b`'s superseded seconds ago are in the grace: nothing pruned
    prune = {"manifest": f"manifests/{b}.m001.json", "grace": [f"manifests/{a}.json", f"manifests/{b}.json"], "runs": [], "objects": 0,
             "bytes": 0, "deleted": 0}
    assert out == {"gen": "g", "after": b, "added": [], "merge": None, "files": 0, "prune": prune, "compact": None}
    assert _files(appended["gen"]) == before


def test_add_prunes_the_folded_runs_after_the_grace(appended, tmp_path):
    """Four scans appended one `add` at a time (one work dir throughout), as if `AGE` s apart (each `add` first ages the
    store's manifests by that much, rather than sleeping), the grace `-G 600`: the 2nd's carry folds the first two runs
    into one; the 3rd's `add` deletes them from the store (the manifests listing them superseded `AGE` s ago; the
    revision, superseded by the 3rd's own, is in the grace), all but their `cdelta/` and `scans.json`, which the 4th's
    append still refolds; the 4th's carry folds everything into one run, its inputs kept for the grace. Base ⊕ the newest
    manifest's runs, and base ⊕ the 4th's own manifest's, read as the rebuild."""
    import os

    root, scans = appended["root"], appended["scans"]
    ids = scan_ids(scans)
    a, b, c, d = ids[-4:]
    store, work = tmp_path / "store", tmp_path / "work"
    with _small_groups():
        _build(root, scans, store, tmp_path / "work-build", through=ids[-5])
    gen = store / sn.PREFIX / "g"
    AGE, GRACE = 700, 600

    def add(s: str) -> dict:
        for p in (gen / "manifests").glob("*.json"):
            t = p.stat().st_mtime - AGE
            os.utime(p, (t, t))
        return _add(root, scans, store, work, through=s, grace_s=GRACE)

    outs = {s: add(s) for s in (a, b)}
    before_c = _files(gen)
    retained = ("cdelta/", "scans.json")
    folded = {r: {k: v for k, v in before_c.items() if k.startswith(f"{r}/") and not k.removeprefix(f"{r}/").startswith(retained)}
              for r in (sa.run_key(a, a), sa.run_key(b, b))}
    outs |= {s: add(s) for s in (c, d)}
    nothing = lambda m, *g: {"manifest": f"manifests/{m}.json", "grace": [f"manifests/{x}.json" for x in g], "runs": [],  # noqa: E731
                             "objects": 0, "bytes": 0, "deleted": 0}
    n = sum(len(f) for f in folded.values())
    size = sum(len(v) for f in folded.values() for v in f.values())
    assert [outs[s]["prune"] for s in (a, b, c, d)] == [
        nothing(a),
        nothing(f"{b}.m001", a, b),  # `a`'s superseded by `b`'s, `b`'s by the revision, both this `add`
        {"manifest": f"manifests/{c}.json", "grace": [f"manifests/{b}.m001.json"],
         "runs": [{"key": r, "objects": len(f), "bytes": sum(len(v) for v in f.values())} for r, f in folded.items()],
         "objects": n, "bytes": size, "deleted": n},
        nothing(f"{d}.m001", c, d),
    ]
    assert sorted(p.name for p in (gen / "manifests").iterdir()) == [f"{a}.json", f"{b}.json", f"{b}.m001.json", f"{c}.json", f"{d}.json", f"{d}.m001.json"]
    newest, grace = (json.loads((gen / "manifests" / f"{m}.json").read_text())["runs"] for m in (f"{d}.m001", d))
    assert [r["key"] for r in newest] == [sa.run_key(a, d)]
    assert [r["key"] for r in grace] == [sa.run_key(a, b), sa.run_key(c, c), sa.run_key(d, d)]
    assert sorted(p.name for p in (gen / "deltas").iterdir()) == sorted(
        sa.run_key(*x).removeprefix("deltas/") for x in ((a, a), (b, b), (a, b), (c, c), (d, d), (a, d)))
    # the folded runs keep only what later appends refold; the work dir's mirror lost the rest too (never uploaded again)
    for r in folded:
        assert sorted(_files(gen / r)) == ["cdelta/r0000.parquet", "cdelta/r0001.parquet", "scans.json"]
    assert sorted(_files(work / "store" / sn.PREFIX / "g")) == sorted(_files(gen))
    # every reader file of the newest and the grace manifests' runs is there
    from dt_cloud.static_merge import missing
    assert missing(sl.LocalRunStore(gen, tmp_path / "lease"), [*newest, *grace]) == []
    full = appended["full"]
    oracle = _oracle(appended["merged"])
    for runs in (newest, grace):
        tiers = [gen / r["key"] for r in runs]
        assert _combine(_sx([gen, *tiers])) == _sx([full])
        tiered = sa.TieredReader([_reader(gen, pq.read_table(gen / "sidecar.parquet")), *(_reader(t, pq.read_table(t / "sidecar.parquet")) for t in tiers)])
        for term in TERMS:
            if len(term) >= 3:
                assert tiered.answer(term, ids)["answers"] == {x: _brute_answer(oracle, term, x) for x in ids}, term
        tiered_cat = sa.TieredCatalog([_catalog(gen / "catalog"), *(_catalog(t / "catalog") for t in tiers)])
        for term in TERMS:
            assert tiered_cat.answer(term, ids) == _catalog(full / "catalog").answer(term, ids), term


def test_add_an_unchanged_scan(fixture, tmp_path):  # noqa: F811
    """A scan identical to the one before it (an idle day: its delta opens and closes nothing) appends as a run, and
    base ⊕ that run reads as the generation rebuilt over every scan."""
    import shutil

    from dt_cloud.scan_id import scan_label

    root, scans, _ = fixture
    last = scans["scans"][-1]
    idle = scan_label(sn.scan_epoch(last["id"]) + 86400)
    root2 = tmp_path / "root"
    shutil.copytree(root, root2)
    src = last["src"].replace(last["id"], idle)
    (root2 / src).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / last["src"], root2 / src)
    scans2 = {**scans, "scans": [*scans["scans"], {**last, "id": idle, "src": src, "ts": sn.scan_epoch(idle)}]}
    _build(root2, scans2, tmp_path / "store", tmp_path / "work", through=last["id"])
    out = _add(root2, scans2, tmp_path / "store", tmp_path / "work-add")
    assert [x["scan"] for x in out["added"]] == [idle]
    _build(root2, scans2, tmp_path / "full", tmp_path / "work-full")
    gen, full = tmp_path / "store" / sn.PREFIX / "g", tmp_path / "full" / sn.PREFIX / "g"
    run = gen / sa.run_key(idle, idle)
    assert _combine(_sx([gen, run])) == _sx([full])
    merged = tmp_path / "check"
    sa.merge_catalogs([gen / "catalog", run / "catalog"], merged)
    for f in ("cells.parquet", "index.parquet"):
        assert (merged / f).read_bytes() == (full / "catalog" / f).read_bytes(), f


def test_url_store_matches_a_local_one(built, tmp_path):
    """A URL store (fsspec `memory://`) gets the same keys and bytes as a local one (but the catalog's timing)."""
    with _small_groups():
        _build(built["root"], built["scans"], f"memory://{tmp_path.name}/store", tmp_path / "work")
    fs = fsspec.filesystem("memory")
    got = {k.split("/store/", 1)[1]: fs.cat_file(k) for k in sorted(fs.find(f"/{tmp_path.name}/store"))}
    want = {f"{sn.PREFIX}/g/{k}": v for k, v in _files(built["gen"]).items()}
    timing = f"{sn.PREFIX}/g/catalog/meta.json"  # its `s` (seconds) differs run to run
    untimed = lambda files: {k: ({**json.loads(v), "s": None} if k == timing else v) for k, v in files.items()}  # noqa: E731
    assert untimed(got) == untimed(want)


def test_publish_order():
    keys = ["manifests/a.json", "deltas/a/drill/meta.json", "deltas/a/meta.json", "deltas/a/sx/s0000.parquet", "deltas/a/catalog/meta.json",
            "deltas/a/drill/long-roots-index.parquet", "sidecar.parquet"]
    assert sl.publish_order(keys) == ["deltas/a/drill/long-roots-index.parquet", "deltas/a/sx/s0000.parquet", "sidecar.parquet",
                                      "deltas/a/catalog/meta.json", "deltas/a/meta.json", "deltas/a/drill/meta.json", "manifests/a.json"]
    # a run's anchored tier: its name index and rollups, the starts-with catalog's marker, then `anchors/meta.json` (with the drill's)
    keys = ["deltas/a/anchors/meta.json", "deltas/a/anchors/start/meta.json", "deltas/a/anchors/rollups/end-s0000.parquet",
            "deltas/a/names/sidecar.parquet", "deltas/a/names/sx/s0000.parquet", "deltas/a/drill/meta.json", "manifests/a.json"]
    assert sl.publish_order(keys) == ["deltas/a/anchors/rollups/end-s0000.parquet", "deltas/a/names/sidecar.parquet", "deltas/a/names/sx/s0000.parquet",
                                      "deltas/a/anchors/start/meta.json", "deltas/a/anchors/meta.json", "deltas/a/drill/meta.json", "manifests/a.json"]


def test_publish_steps():
    """`upload_new`'s steps over `publish_order`: the data files (and only they) together; each marker, `scans.json` and
    each manifest a step of its own, in order."""
    from dt_cloud.blobstore import publish_steps

    keys = ["drill/long/roots/r0000.parquet", "drill/long/roots/r0001.parquet", "scans.json", "sx/s0000.parquet", "catalog/meta.json",
            "deltas/a/meta.json", "drill/meta.json", "manifests/a.json", "manifests/b.json"]
    assert publish_steps(sl.publish_order(keys), sl.publish_tier) == [
        ["drill/long/roots/r0000.parquet", "drill/long/roots/r0001.parquet"], ["scans.json"], ["sx/s0000.parquet"],
        ["catalog/meta.json"], ["deltas/a/meta.json"], ["drill/meta.json"], ["manifests/a.json"], ["manifests/b.json"]]
    assert publish_steps(["a", "b"], None) == [["a"], ["b"]]


def test_upload_new_puts_each_step_after_the_last(tmp_path):
    """`upload_new` with a tier: a step's files are all up before the next step's first starts (data in parallel, a marker
    after every data file before it), and every new file goes up once."""
    import threading
    import time

    from dt_cloud import blobstore as bs

    local = tmp_path / "gen"
    data = [f"drill/long/roots/r{i:04d}.parquet" for i in range(12)]
    for k in [*data, "drill/meta.json", "manifests/a.json", "old.parquet"]:
        (local / k).parent.mkdir(parents=True, exist_ok=True)
        (local / k).write_text(k)
    events, lock, live, peak = [], threading.Lock(), [0], [0]

    class Rec:
        def put(self, src, key):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                events.append(("start", key))
            time.sleep(0.02)
            with lock:
                live[0] -= 1
                events.append(("end", key))

    got = bs.upload_new(Rec(), local, "p", {"p/old.parquet": 1}, sl.publish_order, tier=sl.publish_tier, workers=4)
    assert got == [*data, "drill/meta.json", "manifests/a.json"]
    starts = [k for e, k in events if e == "start"]
    assert sorted(starts) == sorted(f"p/{k}" for k in got)
    pos = {(e, k): i for i, (e, k) in enumerate(events)}
    assert max(pos[("end", f"p/{k}")] for k in data) < pos[("start", "p/drill/meta.json")]
    assert pos[("end", "p/drill/meta.json")] < pos[("start", "p/manifests/a.json")]
    assert peak[0] == 4



def test_disk_peak_per_stage(tmp_path):
    """`DiskPeak`: each `take()` is the peak above the starting level since the previous one; `close()` the run's peak."""
    used = iter([100, 100 + 3 * sl.GB, 100 + sl.GB, 100 + 2 * sl.GB, 100])
    disk = sl.DiskPeak(tmp_path, every_s=3600, usage=lambda: next(used))
    disk.sample()
    assert disk.take() == 3.0
    disk.sample()
    assert disk.close() == 3.0
    assert disk.peak == 2 * sl.GB


def test_query_reads_a_non_gcs_store(built):
    """`static-names query -b <dir>` (as `r2://…`): the base generation's answers through `blobstore.Store`, each the
    brute force's over the scans."""
    ids = scan_ids(built["scans"])
    oracle = _oracle(built["merged"])
    terms = [t for t in TERMS if len(t) >= 3]
    res = CliRunner().invoke(sn.cli, ["query", "-b", str(built["tmp"] / "store"), "-g", "g", *(x for d in ids for x in ("-d", d)), *terms],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    got = [json.loads(ln) for ln in res.stdout.splitlines()]
    assert [o["answers"] for o in got] == [{d: _brute_answer(oracle, t, d) for d in ids} for t in terms]


def test_anchors_rerun_over_a_pinned_manifest_reproduces_a_published_run(appended, tmp_path):
    """`static_anchors run -P manifests/<a>.json -o OUT` (a canary's re-run, specs/stage-canaries.md) after the carry
    merged `a`: over the tiers `a`'s own manifest listed before it, into OUT, nothing uploaded: `a`'s published `anchors/`,
    md5 for md5, in the report."""
    import hashlib

    ids = scan_ids(appended["scans"])
    a, gen = ids[-2], appended["gen"]
    out = tmp_path / "rerun"
    with _small_groups(1):
        # `-b`: its default reads the deployment profile; `-o` never touches a bucket
        res = CliRunner().invoke(an.cli, ["run", "-b", "unused", "-g", "g", "-d", a, "-m", str(gen.parent.parent), "-P", f"manifests/{a}.json", "-o", str(out),
                                          "-T", str(tmp_path / "t"), "-R", str(R), "-K", str(KEEP), "-p", "2", "-M", "1GB"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    report = json.loads((out / "report.json").read_text())
    published = gen / sa.run_key(a, a)
    want = {f.relative_to(published).as_posix(): hashlib.md5(f.read_bytes()).hexdigest() for f in sorted((published / an.ANCHORS).rglob("*")) if f.is_file()}
    assert (report["run"], report["manifest"], report["tiers"]) == (a, f"manifests/{a}.json", 1)
    assert report["files"] == want


def test_heavy_jobs_bounded_by_threads_and_memory(monkeypatch):
    """`heavy_jobs`: `HEAVY_THREADS` threads and `HEAVY_MEM` (of a pool's `limit / (jobs + 1)` shares) each, at least
    1, at most the ranges: a 32-thread, 200 GiB helper runs 4 heavy ranges at once, a 48-thread 270 GiB fold VM 5."""
    import duckdb

    con = duckdb.connect()
    monkeypatch.setattr(sl, "HEAVY_THREADS", 8)
    monkeypatch.setattr(sl, "HEAVY_MEM", 40 << 30)

    def jobs(threads: int, mem: str, ranges: int) -> int:
        con.execute(f"SET threads={threads}")
        con.execute(f"SET memory_limit='{mem}'")
        return sl.heavy_jobs(con, ranges)

    assert [jobs(32, "200GiB", 9), jobs(48, "270GiB", 9), jobs(48, "270GiB", 3), jobs(16, "64GiB", 9), jobs(4, "1GiB", 9)] == [4, 5, 3, 1, 1]

    def beside(threads: int, mem: str, ranges: int) -> int:
        con.execute(f"SET threads={threads}")
        con.execute(f"SET memory_limit='{mem}'")
        return sl.heavy_jobs(con, ranges, beside=True)

    # beside the light ranges, `LIGHT_MEM` (48 GiB) kept for theirs: a 180 GiB helper runs 3, the 270 GiB fold VM 5
    assert [beside(32, "180GiB", 9), beside(48, "270GiB", 9), beside(48, "270GiB", 2), beside(16, "64GiB", 9)] == [3, 5, 2, 1]
    con.close()
