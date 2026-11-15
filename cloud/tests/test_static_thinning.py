"""Static thinning rebuilds every derived index from projected canonical versions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest

from dt_cloud import append_runner as ar
from dt_cloud import static_compact as sk
from dt_cloud import static_local as sl
from dt_cloud import static_names as sn
from dt_cloud.static_thinning import fold_thinned

from test_static_local import FLOOR, KEEP, R, V, _add, _build, _small_groups
from test_static_names import fixture, scan_ids  # noqa: F401


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


@pytest.fixture(scope="module")
def candidates(fixture, tmp_path_factory):  # noqa: F811
    root, scans, _ = fixture
    ids = scan_ids(scans)
    retained = [ids[0], ids[2], ids[-1]]
    tmp = tmp_path_factory.mktemp("static-thinning")
    cases = []
    with _small_groups():
        for appended in (False, True):
            label = "runs" if appended else "base"
            store, work = tmp / label / "store", tmp / label / "work"
            _build(root, scans, store, work / "build", through=ids[1] if appended else None, extra=("-x", "4,2"))
            if appended:
                _add(root, scans, store, work / "add", through=ids[-1])
            source = store / sn.PREFIX / "g"
            _, runs = ar.newest_runs(source)
            source_hashes = _hashes(store)
            base = json.loads((source / "scans.json").read_text())
            doc = {**base, "scans": [
                item for item in [*base["scans"], *sk.run_scans(source, "g", runs)]
                if item["id"] in retained
            ]}
            reference = tmp / label / "reference"
            sl.build_gen(
                doc,
                str(root),
                reference,
                work / "reference",
                gen="thin",
                rule=sn.gen_rule(base),
                shard_rows=60,
                max_rows=V,
                R=R,
                K=KEEP,
                floor=FLOOR,
                threads=2,
                mem="1GB",
                chunk_rows=3,
                ranges=json.loads((source / "ranges.json").read_text()),
            )
            candidate = tmp / label / "candidate"
            with patch.object(sn, "build_range", side_effect=AssertionError("thinning must not read raw scan sources")):
                summary = fold_thinned(
                    source,
                    candidate,
                    work / "thin",
                    gen="g",
                    new_gen="thin",
                    runs=runs,
                    retained=retained,
                    threads=2,
                    mem="1GB",
                    chunk_rows=3,
                )
            cases.append({
                "candidate": candidate,
                "reference": reference,
                "source": source,
                "store": store,
                "source_hashes": source_hashes,
                "summary": summary,
                "retained": retained,
                "ids": ids,
                "runs": runs,
            })
    return cases


def test_static_thinning_matches_retained_scan_rebuild(candidates):
    for case in candidates:
        candidate, reference = case["candidate"], case["reference"]
        got = {path: digest for path, digest in _hashes(candidate).items() if path.endswith(".parquet")}
        want = {path: digest for path, digest in _hashes(reference).items() if path.endswith(".parquet")}
        assert got == want
        for relative in ("scans.json", "ranges.json", "shards.json", "catalog/members.json", "drill/meta.json", "anchors/meta.json"):
            assert json.loads((candidate / relative).read_text()) == json.loads((reference / relative).read_text())
        got_catalog = json.loads((candidate / "catalog" / "meta.json").read_text())
        want_catalog = json.loads((reference / "catalog" / "meta.json").read_text())
        assert {k: v for k, v in got_catalog.items() if k != "s"} == {k: v for k, v in want_catalog.items() if k != "s"}


def test_static_thinning_never_changes_source_or_live_pointers(candidates):
    for case in candidates:
        assert _hashes(case["store"]) == case["source_hashes"]
        assert sorted(path.name for path in case["candidate"].glob("*CURRENT*")) == []
        assert (case["candidate"] / ar.SUCCESSOR).exists() is False


def test_static_thinning_records_exact_retained_set_and_empty_run_stack(candidates):
    for case in candidates:
        candidate, ids = case["candidate"], case["ids"]
        summary = case["summary"]
        assert {key: summary[key] for key in ("from", "gen", "runs", "source_scans", "retained", "removed")} == {
            "from": "g",
            "gen": "thin",
            "runs": [run["key"] for run in case["runs"]],
            "source_scans": ids,
            "retained": case["retained"],
            "removed": [ids[1], ids[3]],
        }
        assert json.loads((candidate / "thinning.json").read_text()) == summary
        assert json.loads((candidate / "manifests" / f"{ids[-1]}.json").read_text()) == {
            "gen": "thin",
            "date": ids[-1],
            "base_scans": 3,
            "scans": case["retained"],
            "runs": [],
        }
        stats = [json.loads(path.read_text()) for path in sorted((candidate / "cstats").glob("*.json"))]
        assert [sum(item["versions"][i] for item in stats) for i in (0, 1)] == summary["versions"]
        assert summary["versions"][1] == sum(pq.read_metadata(path).num_rows for path in (candidate / "cintervals").glob("*.parquet"))


@pytest.mark.parametrize("retained,message", [
    ([], "retained scans must not be empty"),
    (["2026-10-01"], "retained scans must preserve both source endpoints"),
    (["2026-10-01", "2026-10-04"], "retained scans are absent from the source: ['2026-10-04']"),
])
def test_static_thinning_rejects_invalid_retained_sets_before_writing(tmp_path, retained, message):
    source = tmp_path / "source"
    source.mkdir()
    (source / "scans.json").write_text(json.dumps({"scans": [{"id": "2026-10-01", "ts": 1}, {"id": "2026-10-02", "ts": 2}]}) + "\n")
    with pytest.raises(ValueError) as error:
        fold_thinned(source, tmp_path / "candidate", tmp_path / "work", gen="g", new_gen="thin", runs=[], retained=retained)
    assert str(error.value) == message
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source"]


def test_static_thinning_rejects_in_place_and_existing_outputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for candidate in (source, source / "child", tmp_path):
        with pytest.raises(ValueError) as error:
            fold_thinned(source, candidate, tmp_path / "work", gen="g", new_gen="thin", runs=[], retained=[])
        assert str(error.value) == "source and candidate directories must not overlap"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "keep.txt").write_text("existing output\n")
    with pytest.raises(FileExistsError) as error:
        fold_thinned(source, candidate, tmp_path / "work", gen="g", new_gen="thin", runs=[], retained=[])
    assert str(error.value) == f"candidate output is not empty: {candidate}"
    assert (candidate / "keep.txt").read_text() == "existing output\n"
