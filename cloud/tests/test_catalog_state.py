"""The static catalog's carried counts (specs/append-open-state.md): each append's catalog from the previous scan's
`xcount` and the live runs' shards is byte-identical to the refold over every scan's `cdelta` since the base, across
several appends with a carry (a merged run as history) in between, and its `xcount` is the refold's; a state that
isn't trusted (missing, partial, stale, corrupt) is never read."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dt_cloud import static_append as sa
from dt_cloud import static_catalog as sc
from dt_cloud import static_names as sn

from test_static_catalog import _gen
from test_static_names import _read, fixture  # noqa: F401

BASE = 2  # scans in the base; the rest are appended one at a time
MERGE_AFTER = 2  # the first two runs carry into one level-1 run before the third append

FETCHED = re.compile(r"xcount: carried \((\d+) prefixes first kept")


def _files(d: Path) -> list[str]:
    return sorted(str(f) for f in d.glob("*.parquet"))


def _bytes(d: Path) -> dict[str, bytes]:
    return {f: (d / f).read_bytes() for f in ("cells.parquet", "index.parquet")}


@pytest.fixture(scope="module", params=[3, 12])
def chain(request, fixture, tmp_path_factory):  # noqa: F811
    """The scans after the base appended one at a time; per append, the catalog and `xcount` both ways."""
    V = request.param
    root, scans, _ = fixture
    tmp = tmp_path_factory.mktemp(f"xcount-v{V}")
    base = _gen(root, {"bucket": "b", "scans": scans["scans"][:BASE]}, tmp / "base", V)
    con = base["con"]
    shards = sc.BaseShards(str(base["out"]), base["side"])
    ranges = base["ranges"]["ranges"]
    prev = {r["i"]: f"SELECT * FROM read_parquet({sn.q(str(base['build'] / 'cintervals' / f'r{r['i']:04d}.parquet'))})" for r in ranges}
    runs: list[dict] = []  # the live runs, as a manifest lists them (`key` relative to `tmp`)
    deltas: list[list[str]] = []
    steps = []
    logged: list[str] = []
    rg, sa.SX_RG = sa.SX_RG, 4
    err, sc.err = sc.err, lambda *a, **kw: logged.append(" ".join(map(str, a)))
    try:
        for j in range(BASE, len(scans["scans"])):
            scan = scans["scans"][j]
            key = sa.run_key(scan["id"], scan["id"])
            run = tmp / key
            for r in ranges:
                name = f"r{r['i']:04d}"
                sa.append_open(con, prev[r["i"]], scan, r, name, run, bucket="b", mount=str(root))
                prev[r["i"]] = f"SELECT * FROM read_parquet({sn.q(str(run / 'copen' / f'{name}.parquet'))})"
            deltas.append(_files(run / "cdelta"))
            sa.delta_shards(con, deltas[-1], run, target_rows=7)
            tiers = [base["final"], *(tmp / r["key"] / "catalog" for r in runs)]
            # the refold, always; its `xcount` is the oracle
            refold = sa.catalog_delta(con, tiers, shards, deltas, V, run / "catalog-refold", tmp / "work" / f"{j}r",
                                      state_out=run / "state-refold", scan=scan["id"])
            step = {"scan": scan["id"], "run": run, "refold": refold, "state": tmp / "state" / scan["id"] / "catalog"}
            # as `runs catalog` does: the previous scan's state when trusted (the first run past the base has none)
            state, history = sa.carried_inputs(con, tmp / "state", tmp, runs)
            assert (state is None, history is None) == (not steps, not steps)
            step["carried"] = sa.catalog_delta(con, tiers, shards, deltas, V, run / "catalog", tmp / "work" / f"{j}c",
                                               state=state, history=history, state_out=step["state"], scan=scan["id"])
            steps.append(step)
            runs.append({"key": key, "first": scan["id"], "last": scan["id"], "level": 0, "scans": [scan["id"]]})
            if len(runs) == MERGE_AFTER:
                a, b = runs
                m = {"key": sa.run_key(a["first"], b["last"]), "first": a["first"], "last": b["last"], "level": 1, "scans": [*a["scans"], *b["scans"]]}
                sa.merge_shards([tmp / a["key"], tmp / b["key"]], tmp / m["key"], target_rows=9)
                sa.merge_catalogs([tmp / a["key"] / "catalog", tmp / b["key"] / "catalog"], tmp / m["key"] / "catalog", {"max_rows": V})
                runs = [m]
    finally:
        sa.SX_RG = rg
        sc.err = err
    fetched = [int(m.group(1)) for line in logged if (m := FETCHED.match(line))]
    return {"V": V, "con": con, "steps": steps, "tmp": tmp, "fetched": fetched}


def test_carried_catalog_equals_refold(chain):
    """Every append: the run's catalog files are the refold's byte for byte, and so is its `xcount` (rows, file and
    JSON). Every append but the first carries: the second from a level-0 run, the later ones from a merged run."""
    steps = chain["steps"]
    assert len(steps) >= 3
    for s in steps:
        assert _bytes(s["run"] / "catalog") == _bytes(s["run"] / "catalog-refold"), s["scan"]
        assert _read(s["state"] / "xcount.parquet") == _read(s["run"] / "state-refold" / "xcount.parquet"), s["scan"]
        assert (s["state"] / "xcount.parquet").read_bytes() == (s["run"] / "state-refold" / "xcount.parquet").read_bytes()
        assert json.loads((s["state"] / "xcount.json").read_text()) == json.loads((s["run"] / "state-refold" / "xcount.json").read_text())
        assert {k: v for k, v in s["carried"].items() if k != "append"} == {k: v for k, v in s["refold"].items() if k != "append"}
        assert {k: v for k, v in s["carried"]["append"].items() if k != "s"} == {k: v for k, v in s["refold"]["append"].items() if k != "s"}


def test_carried_appends_read_history(chain):
    """The fixture exercises the carried path's two reads of the runs' shards: literals that became members on a carried
    append (their earlier events from the runs), and, at V = 12 (above the base's 5-row groups, so a prefix can be
    evaluated without being kept), prefixes first kept on a carried append (their earlier rows' children counted from
    the runs). At V = 3 nearly every evaluated prefix is kept at once."""
    assert len(chain["fetched"]) == len(chain["steps"]) - 1
    new = sum(s["carried"]["append"]["new_members"] for s in chain["steps"][1:])
    assert new > 0
    if chain["V"] == 12:
        assert sum(chain["fetched"]) > 0


def _state(chain) -> tuple[Path, str, int]:
    s = chain["steps"][1]
    return s["state"], s["scan"], 2


def test_trusted_xcount_accepts_the_written_state(chain):
    d, scan, n = _state(chain)
    assert sc.trusted_xcount(chain["con"], d, scan, n) == d / "xcount.parquet"


def test_untrusted_states_are_refused(chain, tmp_path):
    """A state is read only when its JSON (written last) names the scan and the scans past the base, and the parquet's
    rows, Σ n and digest match it: a stage killed between the two writes, a state left by another scan or a different
    window, and a parquet changed after its JSON are each refused (the caller refolds)."""
    con = chain["con"]
    d, scan, n = _state(chain)
    assert sc.trusted_xcount(con, d, "2026-01-01", n) is None  # another scan's
    assert sc.trusted_xcount(con, d, scan, n + 1) is None  # another window (a compaction, a different base)
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "xcount.parquet").write_bytes((d / "xcount.parquet").read_bytes())
    assert sc.trusted_xcount(con, partial, scan, n) is None  # killed before its JSON
    only_json = tmp_path / "only-json"
    only_json.mkdir()
    (only_json / "xcount.json").write_text((d / "xcount.json").read_text())
    assert sc.trusted_xcount(con, only_json, scan, n) is None
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    (tampered / "xcount.json").write_text((d / "xcount.json").read_text())
    t = pq.read_table(d / "xcount.parquet")
    rows = t.to_pylist()
    rows[0]["n"] += 1
    rows[1]["n"] -= 1  # Σ n unchanged: only the digest catches it
    pq.write_table(pa.Table.from_pylist(rows, schema=t.schema), tampered / "xcount.parquet")
    assert sc.trusted_xcount(con, tampered, scan, n) is None
    truncated = tmp_path / "truncated"
    truncated.mkdir()
    (truncated / "xcount.json").write_text((d / "xcount.json").read_text())
    (truncated / "xcount.parquet").write_bytes((d / "xcount.parquet").read_bytes()[:100])
    assert sc.trusted_xcount(con, truncated, scan, n) is None


def test_a_rewrite_replaces_the_marker_first(chain, tmp_path):
    """`write_xcount` removes an earlier attempt's JSON before writing the parquet, so a rewrite killed midway leaves no
    JSON that could vouch for a half-written parquet (a coordinator that lost its lease mid-stage)."""
    con = chain["con"]
    d, scan, n = _state(chain)
    out = tmp_path / "rewrite"
    out.mkdir()
    (out / "xcount.json").write_text((d / "xcount.json").read_text())
    con.execute("CREATE OR REPLACE TABLE xbad (q VARCHAR, n BIGINT)")

    class Killed(Exception):
        pass

    real = sc.write_sorted

    def killed(*a, **kw):
        raise Killed

    sc.write_sorted = killed
    try:
        with pytest.raises(Killed):
            sc.write_xcount(con, "xbad", out, scan, n)
    finally:
        sc.write_sorted = real
    assert not (out / "xcount.json").exists()
    assert sc.trusted_xcount(con, out, scan, n) is None
