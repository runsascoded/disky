"""Specs for the R2 publish step (`dt_cloud.publish`): exact served-subset
prefixes and the idempotency decision — the pure core the CLI drives."""
import hashlib
from pathlib import Path

from dt_cloud import publish as P
import pytest

MD5_B64 = "1B2M2Y8AsgTpgAmY7PhCfg=="  # md5("") as GCS reports it
MD5_HEX = "d41d8cd98f00b204e9800998ecf8427e"


def test_served_prefixes_are_the_snapshot_and_layer2_dirs():
    # the base layout: no snapshots subdir, tiers under listing/<date>/index/
    assert P.served_prefixes("2026-09-23", subdir="", layer2="listing/{scan}/index/") == ["snapshots/2026-09-23/", "listing/2026-09-23/index/"]
    assert P.served_prefixes("2026-09-23", subdir="r2", layer2="listing/{scan}/index/") == ["snapshots/r2/2026-09-23/", "listing/2026-09-23/index/"]
    # cw: subdir `cw`, layer 2 under cw-l2/<scan>/
    assert P.served_prefixes("2026-09-23T1201", subdir="cw", layer2="cw-l2/{scan}/") == ["snapshots/cw/2026-09-23T1201/", "cw-l2/2026-09-23T1201/"]
    assert P.snapshots_prefix("2026-09-23", "/r2/") == "snapshots/r2/2026-09-23/"


def test_md5_hex_round_trips_gcs_base64():
    assert (P.md5_hex(MD5_B64), P.md5_hex(None), P.md5_hex("")) == (MD5_HEX, None, None)


def test_dest_from_head_prefers_stamped_md5_then_single_part_etag():
    assert P.dest_from_head(None) is None
    stamped = {"ContentLength": 7, "ETag": '"deadbeef-3"', "Metadata": {"gcs-md5": MD5_HEX}}
    assert P.dest_from_head(stamped) == P.Dest(size=7, md5=MD5_HEX)
    single = {"ContentLength": "7", "ETag": f'"{MD5_HEX}"', "Metadata": {}}
    assert P.dest_from_head(single) == P.Dest(size=7, md5=MD5_HEX)
    multipart = {"ContentLength": 7, "ETag": '"deadbeef-3"'}
    assert P.dest_from_head(multipart) == P.Dest(size=7, md5=None)


def test_should_copy_matrix():
    src = P.Obj(key="cw-l2/s/index/g/path-index.parquet", size=10, md5=MD5_HEX)
    cases = [
        (None, True),                                   # absent
        (P.Dest(size=11, md5=MD5_HEX), True),           # size differs
        (P.Dest(size=10, md5="0" * 32), True),          # md5 differs
        (P.Dest(size=10, md5=MD5_HEX), False),          # identical
        (P.Dest(size=10, md5=None), False),             # same size, dest md5 unknown (multipart)
    ]
    assert [P.should_copy(src, dst) for dst, _ in cases] == [want for _, want in cases]
    # a source without an md5 falls back to the size check alone
    assert P.should_copy(P.Obj(key="k", size=10, md5=None), P.Dest(size=10, md5=MD5_HEX)) is False


def test_report_summary_lines():
    r = P.Report(copied=["a", "b"], skipped=["c"], bytes=1_234_567)
    assert r.summary("2026-09-23T1201", dry_run=True) == "publish-r2 2026-09-23T1201: would copy 2 (1,234,567 B), skipped 1 up to date"
    assert r.summary("2026-09-23T1201", dry_run=False) == "publish-r2 2026-09-23T1201: copied 2 (1,234,567 B), skipped 1 up to date"


def test_is_listing_is_a_top_level_parquet_under_the_layer2_dir():
    l2 = "cw-l2/2026-09-23T1201/"
    assert P.is_listing(l2 + "marin-us-east-02a.parquet", l2)
    assert P.is_listing(l2 + "hero-checkpoints.parquet", l2)
    assert not P.is_listing(l2 + "index/20260923T120000Z/path-index.parquet", l2)
    assert not P.is_listing(l2 + "over-time.scans.json", l2)
    assert not P.is_listing("cw-l2/2026-09-22T1201/marin-us-east-02a.parquet", l2)
    # tiers at a layer-2 dir's top level (the base's pre-generation layout) are not listings
    assert not P.is_listing("listing/2026-09-23/index/path-index.parquet", "listing/2026-09-23/index/")
    assert not P.is_listing(l2 + "path-index-coarse24.parquet", l2)
    assert not P.is_listing(l2 + "age-pyramid-1h.parquet", l2)
    assert not P.is_listing(l2 + "over-time.parquet", l2)


def test_gen_dir_names_the_generation_holding_a_key():
    l2 = "cw-l2/2026-09-23T1201/"
    assert P.gen_dir(l2 + "index/20260923T120000Z/path-index.parquet", l2) == l2 + "index/20260923T120000Z"
    assert P.gen_dir(l2 + "marin-us-east-02a.parquet", l2) is None
    assert P.gen_dir("snapshots/cw/2026-09-23T1201/tree.json", l2) is None
    # the base layout: the layer-2 dir is the `index/` itself; loose pre-generation tiers have no gen
    assert P.gen_dir("listing/2026-09-23/index/G1/path-index.parquet", "listing/2026-09-23/index/") == "listing/2026-09-23/index/G1"
    assert P.gen_dir("listing/2026-09-23/index/path-index.parquet", "listing/2026-09-23/index/") is None


SCAN = "2026-09-16T0001"
L2 = f"cw-l2/{SCAN}/"
SRC = [
    P.Obj(key=f"snapshots/cw/{SCAN}/tree.json", size=1, md5=None),
    P.Obj(key=f"{L2}hero-checkpoints.parquet", size=2, md5=None),
    P.Obj(key=f"{L2}index/g1/path-index.parquet", size=10, md5=None),
    P.Obj(key=f"{L2}index/g1/path-index.groups.json", size=3, md5=None),
    P.Obj(key=f"{L2}index/g2/path-index.parquet", size=20, md5=None),
    P.Obj(key=f"{L2}index/g3/over-time.parquet", size=30, md5=None),
]


def test_served_keeps_only_pointed_generations():
    pointed = {f"{L2}index/g2", f"{L2}index/g3/", "cw-l2/other/index/g1"}
    objs, dropped = P.served(SRC, L2, listings=False, pointed=pointed)
    assert [o.key for o in objs] == [
        f"snapshots/cw/{SCAN}/tree.json",
        f"{L2}index/g2/path-index.parquet",
        f"{L2}index/g3/over-time.parquet",
    ]
    assert dropped == {f"{L2}index/g1": (2, 13)}
    # no pointer set: every generation (the `-a` behavior); listings kept by default
    objs, dropped = P.served(SRC, L2)
    assert ([o.key for o in objs], dropped) == ([o.key for o in SRC], {})


def test_publish_dry_run_copies_only_pointed_generations(monkeypatch, capsys):
    monkeypatch.setattr(P, "list_source", lambda bucket, prefixes: list(SRC))
    monkeypatch.setattr(P, "r2_client", lambda: None)
    monkeypatch.setattr(P, "r2_bucket", lambda: "serve")
    monkeypatch.setattr(P, "head_dest", lambda s3, bucket, key: None)
    logged: list[str] = []
    monkeypatch.setattr(P, "err", lambda *a: logged.append(" ".join(map(str, a))))
    r = P.publish(SCAN, subdir="cw", layer2="cw-l2/{scan}/", dry_run=True, listings=False, pointed={f"{L2}index/g2", f"{L2}index/g3"})
    assert r.copied == [f"snapshots/cw/{SCAN}/tree.json", f"{L2}index/g2/path-index.parquet", f"{L2}index/g3/over-time.parquet"]
    assert capsys.readouterr().out.splitlines() == r.copied
    assert logged == [
        f"publish-r2 {SCAN}: left out unpointed generation {L2}index/g1/ (2 objects, 13 B)",
        f"publish-r2 {SCAN}: would copy 3 (51 B), skipped 0 up to date",
    ]


def test_served_small_is_the_snapshot_jsons_and_extras_sidecars():
    l2 = "listing/2026-10-10/index/"
    src = [
        P.Obj(key="snapshots/2026-10-10/age.json", size=1, md5=None),
        P.Obj(key="snapshots/2026-10-10/meta.json", size=1, md5=None),
        P.Obj(key="snapshots/rules.json", size=1, md5=None),
        P.Obj(key=f"{l2}g1/attr.tsv", size=5, md5=None),
        P.Obj(key=f"{l2}g1/attr.tsv.idx.json", size=2, md5=None),
        P.Obj(key=f"{l2}g1/path-index.parquet", size=100, md5=None),
        P.Obj(key=f"{l2}g1/path-index.groups.parquet", size=10, md5=None),
        P.Obj(key=f"{l2}g2/attr.tsv", size=6, md5=None),
        P.Obj(key=f"{l2}g2/path-index-bysize.parquet", size=90, md5=None),
    ]
    objs, dropped = P.served(src, l2, small=True, pointed={f"{l2}g1"})
    assert ([o.key for o in objs], dropped) == (
        [
            "snapshots/2026-10-10/age.json",
            "snapshots/2026-10-10/meta.json",
            "snapshots/rules.json",
            f"{l2}g1/attr.tsv",
            f"{l2}g1/attr.tsv.idx.json",
        ],
        {f"{l2}g2": (1, 6)},
    )
    # cw's layout: the canonical listings and rollups beside the generations stay behind too
    cw = f"cw-l2/{SCAN}/"
    objs, _ = P.served([*SRC, P.Obj(key=f"{cw}index/g2/attr.tsv", size=4, md5=None), P.Obj(key=f"{cw}rollup.parquet", size=9, md5=None)], cw, small=True)
    assert [o.key for o in objs] == [f"snapshots/cw/{SCAN}/tree.json", f"{cw}index/g2/attr.tsv"]


def publication_store(monkeypatch, missing_source=False, corrupt_dest=False):
    obj = P.Obj(key="snapshots/s/meta.json", size=7, md5=MD5_HEX)
    dest = {}
    monkeypatch.setattr(P, "list_source", lambda bucket, prefixes: [] if missing_source else [obj])
    monkeypatch.setattr(P, "r2_client", lambda: None)
    monkeypatch.setattr(P, "r2_bucket", lambda: "serve")
    monkeypatch.setattr(P, "head_dest", lambda s3, bucket, key: dest.get(key))

    def copy(src_bucket, s3, bucket, obj):
        dest[obj.key] = P.Dest(size=7, md5=None if corrupt_dest else MD5_HEX)

    monkeypatch.setattr(P, "copy_one", copy)
    return obj, dest


def test_publish_refuses_missing_required_source_before_copy(monkeypatch):
    obj, dest = publication_store(monkeypatch, missing_source=True)
    with pytest.raises(ValueError) as exc:
        P.publish("s", layer2="listing/{scan}/index/", required=(obj.key,))
    assert str(exc.value) == "publish-r2: missing required source keys: snapshots/s/meta.json"
    assert dest == {}


def test_publish_verifies_required_destination_even_when_skipped(monkeypatch):
    obj, dest = publication_store(monkeypatch)
    first = P.publish("s", layer2="listing/{scan}/index/", required=(obj.key,))
    second = P.publish("s", layer2="listing/{scan}/index/", required=(obj.key,))
    assert first == P.Report(copied=[obj.key], skipped=[], bytes=7)
    assert second == P.Report(copied=[], skipped=[obj.key], bytes=0)
    assert dest == {obj.key: P.Dest(size=7, md5=MD5_HEX)}


def test_publish_refuses_unverified_required_destination(monkeypatch):
    obj, _ = publication_store(monkeypatch, corrupt_dest=True)
    with pytest.raises(ValueError) as exc:
        P.publish("s", layer2="listing/{scan}/index/", required=(obj.key,))
    assert str(exc.value) == "publish-r2: required destination verification failed: snapshots/s/meta.json"


def test_publish_repairs_required_destination_with_unknown_checksum(monkeypatch):
    obj, dest = publication_store(monkeypatch)
    dest[obj.key] = P.Dest(size=7, md5=None)
    assert P.publish("s", layer2="listing/{scan}/index/", required=(obj.key,)) == P.Report(copied=[obj.key], skipped=[], bytes=7)
    assert dest == {obj.key: P.Dest(size=7, md5=MD5_HEX)}


def test_a_local_dir_source_lists_its_files_with_md5s_and_uploads_them(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "snapshots/laptop/S1").mkdir(parents=True)
    (root / "listing/laptop/S1/index/G1").mkdir(parents=True)
    (root / "snapshots/laptop/S1/meta.json").write_text("{}")
    (root / "listing/laptop/S1/index/G1/path-index.parquet").write_bytes(b"pi")
    (root / "listing/laptop/S1/index/G1/.path-index.parquet.tmp").write_bytes(b"partial")
    (root / "listing/laptop/S2/index/G2").mkdir(parents=True)
    (root / "listing/laptop/S2/index/G2/path-index.parquet").write_bytes(b"other scan")
    objs = P.list_source(str(root), ["snapshots/laptop/S1/", "listing/laptop/S1/"])
    assert objs == [
        P.Obj(key="listing/laptop/S1/index/G1/path-index.parquet", size=2, md5=hashlib.md5(b"pi").hexdigest(), content_type=None),
        P.Obj(key="snapshots/laptop/S1/meta.json", size=2, md5=hashlib.md5(b"{}").hexdigest(), content_type="application/json"),
    ]

    class S3:
        def __init__(self):
            self.uploads = []

        def upload_file(self, path, bucket, key, ExtraArgs):
            self.uploads.append((Path(path).read_bytes(), bucket, key, ExtraArgs))

    s3 = S3()
    P.copy_one(str(root), s3, "serve", objs[0])
    assert s3.uploads == [(b"pi", "serve", "listing/laptop/S1/index/G1/path-index.parquet", {"Metadata": {"gcs-md5": hashlib.md5(b"pi").hexdigest()}})]
