"""The interval store's attribution table (specs/attr-interval-table.md): each scan's `attribution.parquet` versioned by
attributing prefix, in the store's key ranges, runs, carries and compaction.

- A base built with it and its runs equal a fresh `build -A` over the same scans, version for version; a compaction and
  a backfill are a fresh build's bytes; the staged chain's state chain gives each run the one-machine `add`'s bytes.
- Every covered scan reads its own map exactly; a covered scan with an empty map reads empty (nothing attributed); an
  uncovered scan (no file) adds no input — a version runs on across it — and is not covered (unknown).
- Without it (cw, r2.rbw.sh, hf, m3), nothing changes: no file, no key, and an attributed store's other files are the
  unattributed store's, byte for byte.
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner
from test_interval_append import _rows, combine
from test_interval_store import V2_FROM, _scan_rows, _universe, _write

from dt_cloud import extras as ex
from dt_cloud import interval_append as ia
from dt_cloud import interval_attribution as iat
from dt_cloud import interval_local as il
from dt_cloud import interval_store as ist
from dt_cloud import scan_retire as sr
from dt_cloud import static_names as sn
from dt_cloud.blobstore import Store

DATES = ["2026-07-30", "2026-07-31", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-04T1236", "2026-08-05", "2026-08-07"]
BASE = 4
LAYOUT = "listing/{id}/path-index.parquet"
RG = 4
AT = ist.ATTRIBUTION
#: Scans with no `attribution.parquet` (a base scan and a run scan): uncovered.
UNCOVERED = ("2026-07-31", "2026-08-04")
#: A covered scan whose map is empty: nothing attributed there.
EMPTY = "2026-08-05"


def attribution_map(j: int) -> dict[str, list]:
    """Scan `j`'s map, `key → [user, source, evidence]`: an evidence change (2), a user change (3), a prefix removed (2,
    3) and back (4), one added (5), a source change at 5 and back after."""
    m = {"b1": ["alice", "manual", None], "b1/gof": ["bob", "wandb-run", "ent/p/1"], "b2/e": ["alice", "rule", "r1"], "b2/f": ["carol", None, None]}
    if j >= 2:
        m["b1/gof"] = ["bob", "wandb-run", "ent/p/2"]
    if j >= 3:
        m["b2/e"] = ["dave", "rule", "r1"]
    if j in (2, 3):
        del m["b2/f"]
    if j >= 5:
        m["b1/d"] = ["erin", "manual", None]
    if j == 5:
        m["b1"] = ["alice", "rule", None]
    return m


def map_rows(j: int) -> list[dict]:
    """Scan `j`'s map as the table's rows at it (`depth, path, user, source, evidence`), sorted by path."""
    if DATES[j] == EMPTY:
        return []
    return ex.attribution_table(attribution_map(j)).to_pylist()


def write_world(root: Path, *, sidecars: bool = False) -> list[dict]:
    """The scans' `path` sorts (`test_interval_local`'s generator) and, beside each covered one, its
    `attribution.parquet` (with `sidecars`, also its `attr.tsv` + block index); returns the scans."""
    rng = random.Random(11)
    paths = _universe(rng)
    scans = []
    for j, d in enumerate(DATES):
        v = 2 if j >= V2_FROM else 1
        key = LAYOUT.replace("{id}", d)
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        _write(_scan_rows(rng, paths, j), root / key, v)
        if d not in UNCOVERED:
            t = ex.attribution_table({} if d == EMPTY else attribution_map(j))
            ex.write_attribution(t, root / key.replace("path-index.parquet", ex.ATTRIBUTION_FILE))
            if sidecars:
                lines = [f"{r['path']}\t{r['user']}\t{r['source'] or ''}\t{r['evidence'] or ''}" for r in t.to_pylist()]
                ex.write_blocked(root / key.replace("path-index.parquet", ex.ATTR_FILE), lines, key_of=lambda line: line.split("\t", 1)[0])
        scans.append({"id": d, "src": key, "ts": sn.scan_epoch(d), "version": v})
    return scans


def build(root: Path, store: Path, work: Path, *extra: str, gen: str = "g", through: str | None = None) -> dict:
    args = ["-g", gen, "-L", LAYOUT, "-s", str(root), "-o", str(store), "-w", str(work), "-k", "3", "-r", str(RG), "-p", "2", "-M", "1GB",
            *(["-t", through] if through else []), *extra]
    res = CliRunner().invoke(il.build_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def add(root: Path, store: Path, work: Path, *extra: str, gen: str = "g") -> dict:
    args = ["-g", gen, "-L", LAYOUT, "-s", str(root), "-o", str(store), "-w", str(work), "-r", str(RG), "-p", "2", "-M", "1GB", *extra]
    res = CliRunner().invoke(il.add_cmd, args, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return json.loads(res.stdout)


def parquets(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*.parquet"))}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("iva")
    return {"root": root, "scans": write_world(root)}


@pytest.fixture(scope="module")
def stores(world, tmp_path_factory):
    """`flagged`: a base built with the table, the rest added (one carry merges the four runs); `opened`: a base without
    it, the rest added with `-A` (the table opens at the first run); `plain`: neither; `fresh`: `build -A` over every
    scan (the reference)."""
    tmp = tmp_path_factory.mktemp("iva-stores")
    root = world["root"]
    out = {}
    for name, b, a in (("flagged", ["-A"], []), ("opened", [], ["-A"]), ("plain", [], [])):
        build(root, tmp / name, tmp / f"w-{name}-b", *b, through=DATES[BASE - 1])
        if name == "opened":
            shutil.copytree(tmp / name, tmp / "opened-base")
        add(root, tmp / name, tmp / f"w-{name}-a", *a)
        out[name] = tmp / name / ist.PREFIX / "g"
    build(root, tmp / "fresh", tmp / "w-fresh", "-A")
    out["fresh"] = tmp / "fresh" / ist.PREFIX / "g"
    out["tmp"] = tmp
    return out


def newest(gen: Path) -> dict:
    keys = sorted(p.name for p in (gen / "manifests").glob("*.json"))
    return json.loads((gen / "manifests" / keys[-1]).read_text())


def table(gen: Path, runs: list[str] | None = None) -> list[dict]:
    """The attribution table's versions: the base's range files and the runs' deltas (default the newest manifest's),
    combined per version."""
    runs = [r["key"] for r in newest(gen)["runs"]] if runs is None and (gen / "manifests").is_dir() else runs or []
    tiers = [_rows(d, AT) for d in (gen, *(gen / k for k in runs)) if (d / AT).is_dir()]
    return combine(tiers, AT)


def live(rows: list[dict], ts: int) -> list[dict]:
    return sorted(({k: r[k] for k in ("depth", "path", "user", "source", "evidence")} for r in rows if r["vf"] <= ts < r["vt"]), key=lambda r: r["path"])


# ── The per-scan input ─────────────────────────────────────────────────────


def test_write_extras_writes_the_attribution_parquet(tmp_path: Path):
    import pandas as pd

    pfx = pd.DataFrame([
        {"key": "b/grug", "user": "calvin", "source": "wandb-run", "evidence": "ent/p/abc"},
        {"key": "b/data", "user": "ryan", "source": "manual", "evidence": None},
        {"key": "b/x/y", "user": "kim", "source": None, "evidence": ""},
        {"key": "b/data/tmp", "user": None, "source": "rule", "evidence": None},  # a nobody rule
    ])
    assert ex.write_extras(pfx, tmp_path) == {"attribution": 4, "attr": 4}
    assert pq.read_table(tmp_path / ex.ATTRIBUTION_FILE).to_pylist() == [
        {"depth": 2, "path": "b/data", "user": "ryan", "source": "manual", "evidence": None},
        {"depth": 3, "path": "b/data/tmp", "user": None, "source": "rule", "evidence": None},
        {"depth": 2, "path": "b/grug", "user": "calvin", "source": "wandb-run", "evidence": "ent/p/abc"},
        {"depth": 3, "path": "b/x/y", "user": "kim", "source": None, "evidence": None},
    ]
    assert pq.read_schema(tmp_path / ex.ATTRIBUTION_FILE) == ex.ATTRIBUTION_SCHEMA
    # the sidecar's lines read back as the same rows (`attribution-convert`)
    back = ex.attribution_from_tsv((tmp_path / ex.ATTR_FILE).read_text())
    assert (back.schema, back.to_pylist()) == (ex.ATTRIBUTION_SCHEMA, pq.read_table(tmp_path / ex.ATTRIBUTION_FILE).to_pylist())


def test_attribution_from_tsv_refuses_unsorted_or_keyless_lines():
    with pytest.raises(ValueError) as e:
        ex.attribution_from_tsv("b/x\tu\t\t\nb/a\tv\t\t\n")
    assert str(e.value) == "line 2: key 'b/a' is not after 'b/x'"
    with pytest.raises(ValueError) as e:
        ex.attribution_from_tsv("\tu\tmanual\t\n")
    assert str(e.value) == "line 1: no key: '\\tu\\tmanual\\t'"


# ── Without the table: nothing changes ─────────────────────────────────────


def test_an_unattributed_store_has_no_table(stores):
    gen = stores["plain"]
    assert [p.relative_to(gen).as_posix() for p in gen.rglob("*") if AT in p.name] == []
    assert AT not in json.loads((gen / "scans.json").read_text())
    assert [m for m in (json.loads(p.read_text()) for p in sorted((gen / "manifests").glob("*.json"))) if AT in m] == []
    assert sorted(p.name for p in (gen / "served").glob("*.parquet")) == sorted(
        f"{s}{x}" for s in ist.served_sorts(True) for x in (".parquet", ".groups.parquet"))
    assert ist.served_sorts(True) == ["path", "bysize", "reads", "slices", "slices-bytotal", "slices-bysize-user"]
    assert ia.tables() == ("pvl", "sv", "svt") and ia.run_sorts() == ("path", "bysize", "slices", "slices-bytotal", "slices-bysize-user")


def test_the_table_leaves_every_other_file_as_it_was(stores):
    """An attributed store's path versions, owner slices, read days and their served sorts — base and runs — are the
    unattributed store's, byte for byte."""
    other = lambda d: {k: v for k, v in parquets(d).items() if f"/{AT}" not in f"/{k}" and not k.startswith(f"{AT}/")}  # noqa: E731
    for name in ("flagged", "opened"):
        assert other(stores[name]) == other(stores["plain"]), name


# ── With it: the versions ──────────────────────────────────────────────────


def test_base_plus_runs_is_a_fresh_build(stores):
    """The flagged store's base and runs (the merged one, and the level-0 runs alone) are `build -A` over every scan,
    version for version."""
    want = table(stores["fresh"], [])
    gen = stores["flagged"]
    assert table(gen) == want
    assert table(gen, [f"deltas/{d}" for d in DATES[BASE:]]) == want


def test_each_covered_scan_reads_its_own_map(world, stores):
    """At every covered scan the table's live rows are the scan's map, row for row; the empty map's scan reads empty;
    the uncovered scans are outside the coverage, in the base's `scans.json` and the manifests alike."""
    for name in ("flagged", "fresh"):
        gen = stores[name]
        rows = table(gen) if name == "flagged" else table(gen, [])
        for j, s in enumerate(world["scans"]):
            if s["id"] not in UNCOVERED:
                assert live(rows, s["ts"]) == map_rows(j), (name, s["id"])
        assert live(rows, sn.scan_epoch(EMPTY)) == []
    flagged = stores["flagged"]
    base = json.loads((flagged / "scans.json").read_text())
    assert [(s["id"], s.get(ist.ATTRIBUTION_SRC)) for s in base["scans"]] == [
        (d, None if d in UNCOVERED else f"listing/{d}/attribution.parquet") for d in DATES[:BASE]]
    assert (base[AT], sorted(ia.covered_scans(base, newest(flagged)))) == (True, [d for d in DATES if d not in UNCOVERED])


def test_a_version_runs_on_across_an_uncovered_scan(stores):
    """`b1` keeps its first value from the first scan to scan 5 through the uncovered scans 1 and 4: one version, closed
    where its source changes; the empty map (6) closes every version, and scan 7 opens them again. `b2/f`, gone at 2–3,
    comes back at the next covered scan (5): the uncovered 4 says nothing."""
    ts = [sn.scan_epoch(d) for d in DATES]
    rows = [(r["path"], r["vf"], r["vt"], r["user"], r["source"]) for r in table(stores["flagged"]) if r["path"] in ("b1", "b2/f")]
    assert rows == [
        ("b1", ts[0], ts[5], "alice", "manual"), ("b1", ts[5], ts[6], "alice", "rule"), ("b1", ts[7], sn.OPEN, "alice", "manual"),
        ("b2/f", ts[0], ts[2], "carol", None), ("b2/f", ts[5], ts[6], "carol", None), ("b2/f", ts[7], sn.OPEN, "carol", None),
    ]


def test_manifests_name_the_runs_covered_scans(stores):
    """Each manifest (and the carry's revision, unchanged) names the run scans the table covers; a store whose base
    lacks the table opens it at its first run, its base scans uncovered."""
    for name in ("flagged", "opened"):
        gen = stores[name]
        got = {p.name: json.loads(p.read_text()).get(AT) for p in sorted((gen / "manifests").glob("*.json"))}
        run_scans = DATES[BASE:]
        want = {f"{d}.json": [x for x in run_scans[:j + 1] if x not in UNCOVERED] for j, d in enumerate(run_scans)}
        want[f"{run_scans[-1]}.m001.json"] = want[f"{run_scans[-1]}.json"]
        assert got == want, name
    opened = stores["opened"]
    base = json.loads((opened / "scans.json").read_text())
    assert (AT in base, [p for p in opened.glob(f"{AT}/*")]) == (False, [])
    rows = table(opened)
    for j, s in enumerate(DATES):
        if j >= BASE and s not in UNCOVERED:
            assert live(rows, sn.scan_epoch(s)) == map_rows(j), s


def test_runs_hold_the_sort_only_with_a_covered_scan(stores):
    """A run of an uncovered scan carries no attribution file (its state carries over); a covered one carries its delta
    and served sort, the empty map's included."""
    gen = stores["flagged"]
    for d in DATES[BASE:]:
        files = sorted(k for k in (p.relative_to(gen / "deltas" / d).as_posix() for p in (gen / "deltas" / d).rglob("*") if p.is_file()) if AT in k)
        want = [] if d in UNCOVERED else [*(f"{AT}/r{i:04d}.parquet" for i in range(3)), f"served/{AT}.groups.parquet", f"served/{AT}.json",
                                          f"served/{AT}.parquet"]
        assert files == sorted(want), d
    meta = {d: json.loads((gen / "deltas" / d / "meta.json").read_text()).get(AT) for d in DATES[BASE:]}
    assert meta == {d: [] if d in UNCOVERED else [d] for d in DATES[BASE:]}


def test_the_staged_state_chain_gives_each_run_the_same_bytes(world, stores, tmp_path):
    """`append_range` over the previous scan's open state (the staged chain's `state/<prev>/`) writes each run's
    attribution delta as the one-machine `add` does, byte for byte."""
    gen = stores["flagged"]
    ranges = json.loads((gen / "ranges.json").read_text())
    con = duckdb.connect()
    prev = None
    for s in world["scans"][BASE:]:
        out = tmp_path / s["id"]
        scan = {**s, **({} if s["id"] in UNCOVERED else {ist.ATTRIBUTION_SRC: f"listing/{s['id']}/attribution.parquet"})}
        for r in ranges["ranges"]:
            name = f"r{r['i']:04d}"
            state = (ia.run_state_sql(str(prev / "state"), name, attributed=True) if prev else ia.base_state_sql(str(gen), name, attributed=True))
            ia.append_range(con, state, scan, r, name, out, bucket="b", mount=str(world["root"]), attributed=True)
        got = {k: v for k, v in parquets(out).items() if k.startswith(f"{AT}/")}
        assert got == {k: v for k, v in parquets(gen / "deltas" / s["id"]).items() if k.startswith(f"{AT}/")}, s["id"]
        prev = out


def test_each_appended_scans_marker_checks_the_table(world, stores):
    """The append's parity marker sums the attribution table's check (a covered scan's map in every range, against the
    published store) beside the path tables'; an uncovered scan's has no attribution check."""
    gen = stores["flagged"]
    for j, d in enumerate(DATES[BASE:], BASE):
        m = json.loads((gen / "verify" / f"{d}.json").read_text())
        n = len(map_rows(j))
        assert (m["pass"], m["tables"].get(AT)) == (True, None if d in UNCOVERED else {"rows": n, "store": n, "missing": 0, "extra": 0}), d


# ── Compaction and backfill: a fresh build's bytes ─────────────────────────


def test_compaction_is_a_fresh_build(world, stores):
    """Compacting the flagged store (its four runs and the base) gives the table a fresh `build -A` writes: the served
    sort byte for byte, the versions row for row, the covered scans named."""
    tmp = stores["tmp"]
    store = tmp / "compact"
    shutil.copytree(tmp / "flagged", store)
    add(world["root"], store, tmp / "w-compact", "-K")
    new = store / ist.PREFIX / "g.c001"
    fresh = stores["fresh"]
    assert (new / "served" / f"{AT}.parquet").read_bytes() == (fresh / "served" / f"{AT}.parquet").read_bytes()
    assert (new / "served" / f"{AT}.groups.parquet").read_bytes() == (fresh / "served" / f"{AT}.groups.parquet").read_bytes()
    assert table(new, []) == table(fresh, [])
    scans = json.loads((new / "scans.json").read_text())
    assert (scans[AT], [s.get(ist.ATTRIBUTION_SRC) for s in scans["scans"]]) == (
        True, [s.get(ist.ATTRIBUTION_SRC) for s in json.loads((fresh / "scans.json").read_text())["scans"]])


def test_backfill_from_an_opened_table_is_a_fresh_build(world, stores):
    """A store whose table opened at its first run, backfilled: a new generation with the table over every scan — a
    fresh `build -A`'s served sort, byte for byte — checked against the runs' table where they cover it."""
    tmp = stores["tmp"]
    store = tmp / "opened"
    res = CliRunner().invoke(iat.backfill_attribution_cmd, ["-g", "g", "-O", "g.a1", "-o", str(store), "-s", str(world["root"]), "-r", str(RG), "-p", "2",
                                                           "-M", "1GB", "-w", str(tmp / "w-backfill")], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    new = store / ist.PREFIX / "g.a1"
    fresh = stores["fresh"]
    assert (new / "served" / f"{AT}.parquet").read_bytes() == (fresh / "served" / f"{AT}.parquet").read_bytes()
    assert table(new, []) == table(fresh, [])
    assert json.loads((new / iat.RECORD).read_text()) == {
        "from": "g", "manifest": f"manifests/{DATES[-1]}.m001.json", "runs": [f"deltas/{DATES[BASE]}_{DATES[-1]}"], "scans": len(DATES),
        "covered": [d for d in DATES if d not in UNCOVERED], "uncovered": list(UNCOVERED), "checked": [d for d in DATES[BASE:] if d not in UNCOVERED]}
    # the path versions and their sorts: the source folded, as a compaction folds it
    other = lambda d: {k: v for k, v in parquets(d).items() if k.startswith("served/") and AT not in k}  # noqa: E731
    assert other(new) == other(fresh)
    assert sr.lineage(Store(str(store)), "g.a1") == ["g.a1", "g"]
    # the source and its pointer are left as they were
    assert (json.loads((store / ist.PREFIX / "CURRENT.json").read_text()), AT in json.loads((store / ist.PREFIX / "g" / "scans.json").read_text())) == (
        {"gen": "g"}, False)


def test_backfill_refuses_a_map_the_runs_disagree_with(world, stores, tmp_path):
    """A scan the runs covered whose map now says otherwise (rewritten after its append) refuses the backfill: the runs
    are the record."""
    root = tmp_path / "scans"
    shutil.copytree(world["root"], root)
    d = DATES[BASE + 1]
    t = ex.attribution_table({**attribution_map(BASE + 1), "b1": ["mallory", "manual", None]})
    ex.write_attribution(t, root / f"listing/{d}/{ex.ATTRIBUTION_FILE}")
    store = tmp_path / "store"
    shutil.copytree(stores["tmp"] / "opened-base", store)
    add(world["root"], store, tmp_path / "w-add", "-A")
    res = CliRunner().invoke(iat.backfill_attribution_cmd, ["-g", "g", "-O", "g.a1", "-o", str(store), "-s", str(root), "-r", str(RG), "-p", "2",
                                                           "-M", "1GB", "-w", str(tmp_path / "w")])
    # `b1`'s range: the one whose `[lo, hi)` over `(depth, path)` holds (1, 'b1')
    ranges = json.loads((store / ist.PREFIX / "g" / "ranges.json").read_text())["ranges"]
    i = next(r["i"] for r in ranges if (r["lo"] is None or tuple(r["lo"]) <= (1, "b1")) and (r["hi"] is None or (1, "b1") < tuple(r["hi"])))
    assert (res.exit_code, res.output.strip().splitlines()[-1]) == (
        1, f"backfill-attribution: the table built from the scans' maps differs from g's at (range, scan) [({i}, '{d}')]")
    assert not (store / ist.PREFIX / "g.a1").exists()


def test_convert_writes_the_parquet_beside_each_sidecar(tmp_path):
    """`attribution-convert`: a scan with an `attr.tsv` and no parquet gets one (the same map); one with a parquet, or
    no sidecar, is left alone."""
    root = tmp_path / "root"
    scans = write_world(root, sidecars=True)
    build(root, root, tmp_path / "w", through=DATES[3])
    for d in ("2026-07-30", "2026-08-02"):
        (root / f"listing/{d}/{ex.ATTRIBUTION_FILE}").unlink()
    res = CliRunner().invoke(iat.convert_cmd, ["-g", "g", "-o", str(root)], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout) == {"gen": "g", "scans": {"2026-07-30": "converted 4", "2026-07-31": "none", "2026-08-02": "converted 3",
                                                           "2026-08-03": "has"}}
    for j in (0, 2):
        assert pq.read_table(root / f"listing/{scans[j]['id']}/{ex.ATTRIBUTION_FILE}").to_pylist() == map_rows(j)


# ── Parity (verify, retire) ────────────────────────────────────────────────


def test_attribution_parity_against_each_scans_map(world, stores, tmp_path):
    """`attribution_parity` (verify's whole-map check) over the flagged store's served sorts: every covered scan equal;
    a map with a prefix changed shows it missing and the store's row extra."""
    gen = stores["flagged"]
    runs = [str(gen / r["key"] / "served") for r in newest(gen)["runs"]]
    con = duckdb.connect()
    for j, s in enumerate(world["scans"]):
        if s["id"] in UNCOVERED:
            continue
        f = world["root"] / f"listing/{s['id']}/{ex.ATTRIBUTION_FILE}"
        n = len(map_rows(j))
        assert iat.attribution_parity(con, str(f), str(gen / "served"), runs, s["ts"]) == {"rows": n, "store": n, "missing": 0, "extra": 0}, s["id"]
    bad = tmp_path / "bad.parquet"
    ex.write_attribution(ex.attribution_table({**attribution_map(7), "b1": ["mallory", "manual", None]}), bad)
    n = len(map_rows(7))
    assert iat.attribution_parity(con, str(bad), str(gen / "served"), runs, sn.scan_epoch(DATES[7])) == {"rows": n, "store": n, "missing": 1, "extra": 1}


def test_retire_classes_and_vouching():
    assert [sr.classify(n) for n in ("attr.tsv", "attr.tsv.idx.json", "attr.tsv.gz", ex.ATTRIBUTION_FILE, "ck.txt")] == [
        "attribution", "attribution", "attribution", "attribution", "extras"]
    eq = {"rows": 3, "store": 3, "missing": 0, "extra": 0}
    assert [sr.attribution_vouched(m) for m in (
        {"pass": True, "how": "append", "tables": {"pvl": eq, AT: eq}},
        {"pass": True, "views": 1, "view_eq": 1, "diffs": 0, "diff_eq": 0, "attribution": eq},
        {"pass": True, "how": "append", "tables": {"pvl": eq}},
        {"pass": False, "how": "append", "tables": {"pvl": eq, AT: eq}},
        {"pass": True, "how": "append", "tables": {"pvl": eq, AT: {**eq, "missing": 1}}},
    )] == [True, True, False, False, False]


# ── The staged chain's settings ────────────────────────────────────────────


def test_prepare_pins_the_scans_map(world, tmp_path):
    """`prepare` with `append.attribution`: the run's pinned `scans.json` says so and names the scan's map; an uncovered
    scan's names none; a lineage that carries the table pins the flag without it."""
    root = tmp_path / "root"
    shutil.copytree(world["root"], root)
    build(root, root, tmp_path / "w", through=DATES[BASE - 1])
    pin = lambda d: json.loads((root / ist.PREFIX / "g" / "deltas" / d / "scans.json").read_text())  # noqa: E731
    ia.prepare_scan(str(root), "g", DATES[BASE], (LAYOUT,), True)
    assert (pin(DATES[BASE])[AT], ist.ATTRIBUTION_SRC in pin(DATES[BASE])["scans"][0]) == (True, False)  # 2026-08-04: uncovered
    root2 = tmp_path / "root2"
    shutil.copytree(world["root"], root2)
    build(root2, root2, tmp_path / "w2", "-A", through=DATES[BASE - 1])
    ia.prepare_scan(str(root2), "g", DATES[BASE], (LAYOUT,))
    p = json.loads((root2 / ist.PREFIX / "g" / "deltas" / DATES[BASE] / "scans.json").read_text())
    assert p[AT] is True
    shutil.rmtree(root2 / ist.PREFIX / "g" / "deltas")
    # the base's table carries on; a covered scan is named
    (root2 / f"listing/{DATES[BASE]}/{ex.ATTRIBUTION_FILE}").write_bytes((root2 / f"listing/{DATES[0]}/{ex.ATTRIBUTION_FILE}").read_bytes())
    ia.prepare_scan(str(root2), "g", DATES[BASE], (LAYOUT,))
    p = json.loads((root2 / ist.PREFIX / "g" / "deltas" / DATES[BASE] / "scans.json").read_text())
    assert (p[AT], p["scans"][0][ist.ATTRIBUTION_SRC]) == (True, f"listing/{DATES[BASE]}/{ex.ATTRIBUTION_FILE}")


def test_the_profile_flag(tmp_path, monkeypatch):
    for k in ia.ENV.values():
        monkeypatch.delenv(k, raising=False)
    doc = {"name": "t", "bucket": "b", "append": {"gen": "g", "scratch": "s", "layouts": [LAYOUT], "region": "r", "image": "i", "sa": "a", "r2_bucket": "rb"}}
    p = tmp_path / "p.json"
    p.write_text(json.dumps(doc))
    assert ia.load_config(str(p)).attribution is False
    p.write_text(json.dumps({**doc, "append": {**doc["append"], "attribution": True}}))
    assert ia.load_config(str(p)).attribution is True
    p.write_text(json.dumps({**doc, "append": {**doc["append"], "attribution": "yes"}}))
    with pytest.raises(SystemExit) as e:
        ia.load_config(str(p))
    assert str(e.value) == "interval store: the profile's `append.attribution` must be true or false, not 'yes'"
