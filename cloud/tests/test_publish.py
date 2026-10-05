"""Specs for the R2 publish step (`dt_cloud.publish`): exact served-subset
prefixes and the idempotency decision — the pure core the CLI drives."""
from dt_cloud import publish as P

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
