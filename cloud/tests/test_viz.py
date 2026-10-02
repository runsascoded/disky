"""End-to-end test of `write_path_index` on a tiny synthetic listing: the
path store (specs/path-store.md §4.3) — every dir row per owner slice, every
object row attributed to its dir, in the layer-2's names, cut into the `path`
/ `bysize` sorts (+ the by-user copies when attributing) — plus the age strata
and meta JSONs.
"""

import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from dt_cloud.viz import write_path_index

STORE_COLS = [
    "path", "usr", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
    "sum_storage_class_id_2", "sum_storage_class_id_3", "sum_storage_class_id_4",
]


def _rows(df: pd.DataFrame, cols: list[str]) -> list[tuple]:
    """`cols` per row as tuples in file order, a NULL as None."""
    return [tuple(None if pd.isna(v) else v for v in row) for row in df[cols].itertuples(index=False)]


def _kv(path: Path) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in pq.read_metadata(path).metadata.items() if k != b"ARROW:schema"}

IDENTITIES_YAML = """\
users:
  ryan-williams:
    aliases: [rw]
prefix_owners:
  - prefix: gs://b1/datasets/
    user: data-team
"""

GB = 10**9

TS = {
    "d0615": dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc),
    "d0701": dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc),
    "d0702": dt.datetime(2026, 7, 2, tzinfo=dt.timezone.utc),
    "d0703": dt.datetime(2026, 7, 3, tzinfo=dt.timezone.utc),
    "d0720": dt.datetime(2026, 7, 20, 6, tzinfo=dt.timezone.utc),  # the scan date itself
}

epoch_day = lambda ts: int(ts.timestamp() // 86400)  # noqa: E731

def wmean_day(*pairs: tuple[int, dt.datetime]) -> int:
    """Bytes-weighted mean created date in epoch days, mirroring `_date_of`."""
    return int(sum(b * ts.timestamp() for b, ts in pairs) / sum(b for b, _ in pairs) / 86400)


@pytest.fixture
def listing(tmp_path: Path) -> str:
    path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1"] * 4,
            "name": [
                "users/rw/ckpt/model.bin",
                "users/rw/ckpt/opt.bin",
                "datasets/raw/part0",
                "top.bin",
            ],
            "size_bytes": [100 * GB, 50 * GB, 200 * GB, 30 * GB],
            "created": [TS["d0701"], TS["d0703"], TS["d0615"], TS["d0702"]],
            "storage_class_id": [1, 1, 2, 1],
        }
    ).to_parquet(path)
    return str(path)


@pytest.fixture
def attribution(tmp_path: Path) -> str:
    path = tmp_path / "attribution.parquet"
    pd.DataFrame(
        {
            "prefix": ["gs://b1/users/rw/"],
            "user": ["rw"],
            "source": ["user-prefix"],
            "asof": [dt.date(2026, 7, 20)],
        }
    ).to_parquet(path)
    return str(path)


def test_write_path_index_attr(tmp_path: Path, listing: str, attribution: str):
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"

    pidx = tmp_path / "path-index.parquet"
    meta = write_path_index((listing,), out, "2026-07-20", (attribution,), identities_path, path_index=pidx)

    # `published` is the wall clock at publish time (the site's timestamp shape)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", meta.pop("published"))
    assert meta == {
        "asof": "2026-07-20",
        "generated": dt.date.today().isoformat(),
        "total_bytes": 380 * GB,
        "total_objects": 4,
        "class_bytes": {1: 180 * GB, 2: 200 * GB},
        "index": {"rows": 12, "sorts": {v: {"rows": 12, "groups": 1} for v in ("path", "user", "bysize", "bysize-user")}},
        "users": [{"u": "data-team", "b": 200 * GB}, {"u": "ryan-williams", "b": 150 * GB}],
        "user_class_bytes": {"data-team": {2: 200 * GB}, "ryan-williams": {1: 150 * GB}},
    }

    # The store's dir rows: every ancestor path at every depth, one row per
    # owner slice, descendant-inclusive and exact; its object rows: the
    # listing, each attributed to its dir's owner.
    df = pd.read_parquet(pidx)
    rows = {(r.path, r.usr if isinstance(r.usr, str) else None): (r.kind, int(r.size), int(r.n_files)) for r in df.itertuples()}
    assert rows == {
        ("b1", "data-team"): ("dir", 200 * GB, 1),
        ("b1", "ryan-williams"): ("dir", 150 * GB, 2),
        ("b1", None): ("dir", 30 * GB, 1),
        ("b1/datasets", "data-team"): ("dir", 200 * GB, 1),
        ("b1/datasets/raw", "data-team"): ("dir", 200 * GB, 1),
        ("b1/users", "ryan-williams"): ("dir", 150 * GB, 2),
        ("b1/users/rw", "ryan-williams"): ("dir", 150 * GB, 2),
        ("b1/users/rw/ckpt", "ryan-williams"): ("dir", 150 * GB, 2),
        ("b1/top.bin", None): ("file", 30 * GB, 1),
        ("b1/datasets/raw/part0", "data-team"): ("file", 200 * GB, 1),
        ("b1/users/rw/ckpt/model.bin", "ryan-williams"): ("file", 100 * GB, 1),
        ("b1/users/rw/ckpt/opt.bin", "ryan-williams"): ("file", 50 * GB, 1),
    }
    assert sorted(df.depth.unique().tolist()) == [1, 2, 3, 4, 5]

    age = json.loads((out / "age.json").read_text())
    assert sorted(age, key=lambda r: (r["d"], r["d1"])) == [
        {"d": epoch_day(TS["d0615"]), "d1": "datasets", "u": "data-team", "b": 200 * GB, "o": 1},
        {"d": epoch_day(TS["d0701"]), "d1": "users", "u": "ryan-williams", "b": 100 * GB, "o": 1},
        {"d": epoch_day(TS["d0702"]), "d1": "(files)", "b": 30 * GB, "o": 1},
        {"d": epoch_day(TS["d0703"]), "d1": "users", "u": "ryan-williams", "b": 50 * GB, "o": 1},
    ]


@pytest.fixture
def access(tmp_path: Path) -> str:
    """Layer-2a access agg: one read prefix (plus its ancestor rollup row), and
    a read of `datasets` dated ON the scan date — which the as-of rule
    (`day < scan date`) must leave out, so `datasets` stays never-read."""
    path = tmp_path / "access.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b1", "b1"],
            "path": ["users/rw/ckpt", "users", "datasets"],
            "day": [TS["d0703"].date(), TS["d0703"].date(), TS["d0720"].date()],
            "op": ["GET", "GET", "GET"],
            "last_ts": [TS["d0703"], TS["d0703"], TS["d0720"]],
            "n_ops": [3, 3, 9],
            "bytes_out": [1000, 1000, 9000],
        }
    ).to_parquet(path)
    return str(path)


def test_access_rows_on_or_after_scan_date_are_excluded(tmp_path: Path, listing: str, attribution: str, access: str):
    """A scan dated D aggregates access-log rows with `day < D` only, whatever
    shards exist when it runs — so a re-aggregation of an old date reproduces
    it rather than leaking later reads in. The fixture's `datasets` read is
    dated on the scan date: the access window, the tree and the path index
    all behave as if it never happened."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    pidx = tmp_path / "path-index.parquet"
    meta = write_path_index((listing,), out, "2026-07-20", (attribution,), identities_path, access=(access,), path_index=pidx)
    rd = epoch_day(TS["d0703"])
    assert meta["access"] == {"from": rd, "to": rd}
    a_by_path = pd.read_parquet(pidx).groupby("path")["last_read"].max().to_dict()
    assert pd.isna(a_by_path["b1/datasets"])
    assert a_by_path["b1"] == rd


def test_age_rows_carry_last_read(tmp_path: Path, listing: str, attribution: str, access: str):
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    meta = write_path_index((listing,), out, "2026-07-20", (attribution,), identities_path, access=(access,))
    rd = epoch_day(TS["d0703"])
    assert meta["access"] == {"from": rd, "to": rd}
    # Strata whose dir was read carry `a` (its last-read epoch day); the rest
    # omit it — the site's read axis colors those "never read".
    age = json.loads((out / "age.json").read_text())
    assert sorted(age, key=lambda r: (r["d"], r["d1"])) == [
        {"d": epoch_day(TS["d0615"]), "d1": "datasets", "u": "data-team", "b": 200 * GB, "o": 1},
        {"d": epoch_day(TS["d0701"]), "d1": "users", "u": "ryan-williams", "a": rd, "b": 100 * GB, "o": 1},
        {"d": epoch_day(TS["d0702"]), "d1": "(files)", "b": 30 * GB, "o": 1},
        {"d": epoch_day(TS["d0703"]), "d1": "users", "u": "ryan-williams", "a": rd, "b": 50 * GB, "o": 1},
    ]


def test_path_index_carries_read_day(tmp_path: Path, listing: str, attribution: str, access: str):
    """Dir rows carry subtree-MAX `last_read` (epoch day): a read on
    `users/rw/ckpt` lights up every ancestor up to the bucket, while a
    never-read sibling (`datasets`) stays NULL; the access log is per dir, so
    object rows are NULL."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    pidx = tmp_path / "path-index.parquet"
    write_path_index((listing,), out, "2026-07-20", (attribution,), identities_path, access=(access,), path_index=pidx)
    rd = epoch_day(TS["d0703"])
    df = pd.read_parquet(pidx)
    assert list(df.columns) == STORE_COLS
    # `last_read` is per (path) — collapse the usr slices to the path's max.
    a_by_path = df[df.kind == "dir"].groupby("path")["last_read"].max().to_dict()
    assert a_by_path["b1"] == rd            # bucket: max over everything under it
    assert a_by_path["b1/users"] == rd      # read subtree
    assert a_by_path["b1/users/rw/ckpt"] == rd
    assert pd.isna(a_by_path["b1/datasets"])  # never read → NULL
    assert df[df.kind == "file"]["last_read"].isna().all()


def test_store_sorts(tmp_path: Path, listing: str, attribution: str):
    """The four sorts hold the same 12 rows — 8 dir slices + 4 objects — in
    the layer-2's names then the wire aliases: `path` by `(depth, path, usr)`,
    `bysize` by size bucket desc then path (200 and 150 GB share 2^37; 100 →
    2^36; 50 → 2^35; 30 → 2^34), the by-user copies led by `usr` (NULL first,
    the engine's order). Structural counts are per path on every slice; the
    two native stamps are the listing's `created` (no `updated` here)."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    pidx = tmp_path / "path-index.parquet"
    write_path_index((listing,), out, "2026-07-20", (attribution,), identities_path, path_index=pidx)
    E = {k: int(v.timestamp()) for k, v in TS.items()}
    RW, DT = "ryan-williams", "data-team"

    path = pd.read_parquet(pidx)
    assert list(path.columns) == STORE_COLS
    assert _kv(pidx) == {"tier": "path", "sort": "depth,path,usr"}
    assert _rows(path, ["path", "usr", "kind", "size", "depth", "n_files", "n_children", "n_desc", "mtime", "created"]) == [
        ("b1", None, "dir", 30 * GB, 1, 1, 3, 9, E["d0702"], E["d0702"]),
        ("b1", DT, "dir", 200 * GB, 1, 1, 3, 9, E["d0615"], E["d0615"]),
        ("b1", RW, "dir", 150 * GB, 1, 2, 3, 9, E["d0703"], E["d0703"]),
        ("b1/datasets", DT, "dir", 200 * GB, 2, 1, 1, 2, E["d0615"], E["d0615"]),
        ("b1/top.bin", None, "file", 30 * GB, 2, 1, 0, 0, E["d0702"], E["d0702"]),
        ("b1/users", RW, "dir", 150 * GB, 2, 2, 1, 4, E["d0703"], E["d0703"]),
        ("b1/datasets/raw", DT, "dir", 200 * GB, 3, 1, 1, 1, E["d0615"], E["d0615"]),
        ("b1/users/rw", RW, "dir", 150 * GB, 3, 2, 1, 3, E["d0703"], E["d0703"]),
        ("b1/datasets/raw/part0", DT, "file", 200 * GB, 4, 1, 0, 0, E["d0615"], E["d0615"]),
        ("b1/users/rw/ckpt", RW, "dir", 150 * GB, 4, 2, 2, 2, E["d0703"], E["d0703"]),
        ("b1/users/rw/ckpt/model.bin", RW, "file", 100 * GB, 5, 1, 0, 0, E["d0701"], E["d0701"]),
        ("b1/users/rw/ckpt/opt.bin", RW, "file", 50 * GB, 5, 1, 0, 0, E["d0703"], E["d0703"]),
    ]
    # Storage classes: the one COLDLINE object and its ancestors' data-team slice.
    assert _rows(path[path.sum_storage_class_id_2 > 0], ["path", "usr", "sum_storage_class_id_2"]) == [
        ("b1", DT, 200 * GB), ("b1/datasets", DT, 200 * GB), ("b1/datasets/raw", DT, 200 * GB), ("b1/datasets/raw/part0", DT, 200 * GB),
    ]
    assert (path.sum_storage_class_id_3 == 0).all() and (path.sum_storage_class_id_4 == 0).all()
    # `mtime_mean`: an object's own stamp; a slice's byte-weighted mean of its objects'.
    mm = {(r.path, r.usr if isinstance(r.usr, str) else None): r.mtime_mean for r in path.itertuples()}
    assert mm[("b1/users/rw/ckpt/model.bin", RW)] == E["d0701"]
    assert mm[("b1", None)] == E["d0702"]
    assert mm[("b1/users/rw/ckpt", RW)] == pytest.approx((100 * E["d0701"] + 50 * E["d0703"]) / 150)

    bysize = pd.read_parquet(pidx.with_name("path-index-bysize.parquet"))
    assert list(bysize.columns) == STORE_COLS
    assert _kv(pidx.with_name("path-index-bysize.parquet")) == {"tier": "bysize", "sort": "size_bucket desc,path,usr", "bucket": "log2"}
    assert _rows(bysize, ["path", "usr", "size"]) == [
        ("b1", DT, 200 * GB), ("b1", RW, 150 * GB),
        ("b1/datasets", DT, 200 * GB), ("b1/datasets/raw", DT, 200 * GB), ("b1/datasets/raw/part0", DT, 200 * GB),
        ("b1/users", RW, 150 * GB), ("b1/users/rw", RW, 150 * GB), ("b1/users/rw/ckpt", RW, 150 * GB),
        ("b1/users/rw/ckpt/model.bin", RW, 100 * GB),
        ("b1/users/rw/ckpt/opt.bin", RW, 50 * GB),
        ("b1", None, 30 * GB), ("b1/top.bin", None, 30 * GB),
    ]

    by_user = pd.read_parquet(pidx.with_name("path-index-by-user.parquet"))
    assert _kv(pidx.with_name("path-index-by-user.parquet")) == {"tier": "path", "sort": "usr,depth,path"}
    assert _rows(by_user, ["usr", "path"]) == [
        (None, "b1"), (None, "b1/top.bin"),
        (DT, "b1"), (DT, "b1/datasets"), (DT, "b1/datasets/raw"), (DT, "b1/datasets/raw/part0"),
        (RW, "b1"), (RW, "b1/users"), (RW, "b1/users/rw"), (RW, "b1/users/rw/ckpt"), (RW, "b1/users/rw/ckpt/model.bin"), (RW, "b1/users/rw/ckpt/opt.bin"),
    ]
    bysize_user = pd.read_parquet(pidx.with_name("path-index-bysize-by-user.parquet"))
    assert _kv(pidx.with_name("path-index-bysize-by-user.parquet")) == {"tier": "bysize", "sort": "usr,size_bucket desc,path", "bucket": "log2"}
    assert _rows(bysize_user, ["usr", "path"]) == [
        (None, "b1"), (None, "b1/top.bin"),
        (DT, "b1"), (DT, "b1/datasets"), (DT, "b1/datasets/raw"), (DT, "b1/datasets/raw/part0"),
        (RW, "b1"), (RW, "b1/users"), (RW, "b1/users/rw"), (RW, "b1/users/rw/ckpt"), (RW, "b1/users/rw/ckpt/model.bin"), (RW, "b1/users/rw/ckpt/opt.bin"),
    ]
    # A footer sidecar beside each sort: one group of 12 rows, sizes 30..200 GB.
    for name in ("path-index", "path-index-by-user", "path-index-bysize", "path-index-bysize-by-user"):
        doc = json.loads(pidx.with_name(f"{name}.groups.json").read_text())
        assert [e["name"] for e in doc["schema"][1:]] == STORE_COLS
        (g,) = doc["groups"]
        assert (g[0], g[5], g[8], g[9], g[11]) == (0, 200 * GB, 0, 12, 30 * GB)
    assert not pidx.with_name(".store.parquet").exists()
    assert sorted(p.name for p in tmp_path.glob("path-index*")) == [
        "path-index-by-user.groups.json", "path-index-by-user.groups.parquet", "path-index-by-user.parquet",
        "path-index-bysize-by-user.groups.json", "path-index-bysize-by-user.groups.parquet", "path-index-bysize-by-user.parquet",
        "path-index-bysize.groups.json", "path-index-bysize.groups.parquet", "path-index-bysize.parquet",
        "path-index.groups.json", "path-index.groups.parquet", "path-index.parquet",
    ]


def test_write_path_index_plain(tmp_path: Path, listing: str):
    """Without attribution: one row per dir (no owner slices) + the objects,
    `usr` NULL on every row, the two sorts only (no by-user copies)."""
    out = tmp_path / "out"
    pidx = tmp_path / "idx" / "path-index.parquet"
    meta = write_path_index((listing,), out, "2026-07-20", path_index=pidx)
    assert "users" not in meta
    assert meta["total_bytes"] == 380 * GB
    assert meta["index"] == {"rows": 10, "sorts": {"path": {"rows": 10, "groups": 1}, "bysize": {"rows": 10, "groups": 1}}}
    df = pd.read_parquet(pidx)
    assert list(df.columns) == STORE_COLS
    assert df.usr.isna().all()
    assert _rows(df, ["path", "kind", "size"]) == [
        ("b1", "dir", 380 * GB),
        ("b1/datasets", "dir", 200 * GB), ("b1/top.bin", "file", 30 * GB), ("b1/users", "dir", 150 * GB),
        ("b1/datasets/raw", "dir", 200 * GB), ("b1/users/rw", "dir", 150 * GB),
        ("b1/datasets/raw/part0", "file", 200 * GB), ("b1/users/rw/ckpt", "dir", 150 * GB),
        ("b1/users/rw/ckpt/model.bin", "file", 100 * GB), ("b1/users/rw/ckpt/opt.bin", "file", 50 * GB),
    ]
    assert sorted(p.name for p in pidx.parent.glob("path-index*")) == [
        "path-index-bysize.groups.json", "path-index-bysize.groups.parquet", "path-index-bysize.parquet",
        "path-index.groups.json", "path-index.groups.parquet", "path-index.parquet",
    ]
    age = json.loads((out / "age.json").read_text())
    assert sorted(age, key=lambda r: (r["d"], r["d1"])) == [
        {"d": epoch_day(TS["d0615"]), "d1": "datasets", "b": 200 * GB, "o": 1},
        {"d": epoch_day(TS["d0701"]), "d1": "users", "b": 100 * GB, "o": 1},
        {"d": epoch_day(TS["d0702"]), "d1": "(files)", "b": 30 * GB, "o": 1},
        {"d": epoch_day(TS["d0703"]), "d1": "users", "b": 50 * GB, "o": 1},
    ]


def test_deeper_nobody_rule_overrides_user_prefix(tmp_path: Path):
    # A `user: ~` (explicit nobody) prefix nested INSIDE a user prefix must win
    # for its subtree (deepest-prefix-wins is row-wise: the deeper row's NULL
    # user must not be skipped in favor of the shallower row's user).
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(
        """\
users:
  ryan-williams:
    aliases: [rw]
prefix_owners:
  - prefix: gs://b1/users/rw/shared/
    user: ~
"""
    )
    listing_path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1", "b1"],
            "name": ["users/rw/own/a.bin", "users/rw/shared/b.bin"],
            "size_bytes": [100 * GB, 60 * GB],
            "created": [TS["d0701"], TS["d0702"]],
            "storage_class_id": [1, 1],
        }
    ).to_parquet(listing_path)
    attribution_path = tmp_path / "attribution.parquet"
    pd.DataFrame(
        {
            "prefix": ["gs://b1/users/rw/"],
            "user": ["rw"],
            "source": ["user-prefix"],
            "asof": [dt.date(2026, 7, 20)],
        }
    ).to_parquet(attribution_path)
    out = tmp_path / "out"
    pidx = tmp_path / "path-index.parquet"
    write_path_index((str(listing_path),), out, "2026-07-28", (str(attribution_path),), identities_path, path_index=pidx)
    df = pd.read_parquet(pidx)
    b1 = {(r.usr if isinstance(r.usr, str) else None): int(r.size) for r in df[df.path == "b1"].itertuples()}
    assert b1 == {"ryan-williams": 100 * GB, None: 60 * GB}  # the 60 GB under shared/ is nobody's
    # The objects follow their dirs: `shared/b.bin` is nobody's too.
    assert _rows(df[df.kind == "file"], ["path", "usr"]) == [("b1/users/rw/own/a.bin", "ryan-williams"), ("b1/users/rw/shared/b.bin", None)]


def test_dir_cache_roundtrip(tmp_path: Path, listing: str, attribution: str):
    """Cold run writes the layer-2 cache; a warm run (same cache dir, listing
    unreadable to prove it isn't touched) produces byte-identical outputs."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    cache = tmp_path / "dir-cache"

    cold_out = tmp_path / "cold"
    write_path_index((listing,), cold_out, "2026-07-20", (attribution,), identities_path, dir_cache=cache)
    assert sorted(p.name for p in cache.iterdir()) == ["age-days.parquet", "dir-stats.parquet"]

    # Warm: point the listing at a copy that we then corrupt — object rows must
    # not be read. (The listing arg is still parsed for schema, so keep a valid
    # parquet with different contents: one giant bogus row.)
    bogus = tmp_path / "bogus-listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["b1"],
            "name": ["SHOULD_NOT_BE_READ"],
            "size_bytes": [1],
            "created": [TS["d0701"]],
            "storage_class_id": [1],
        }
    ).to_parquet(bogus)
    warm_out = tmp_path / "warm"
    write_path_index((str(bogus),), warm_out, "2026-07-20", (attribution,), identities_path, dir_cache=cache)

    assert (warm_out / "age.json").read_bytes() == (cold_out / "age.json").read_bytes()
    # meta.json carries the publish wall clock, so the two runs differ there and nowhere else
    warm_meta, cold_meta = (json.loads((d / "meta.json").read_text()) for d in (warm_out, cold_out))
    warm_meta.pop("published"), cold_meta.pop("published")
    assert warm_meta == cold_meta
    meta = warm_meta
    assert meta["total_bytes"] == 380 * GB  # cache content won, bogus listing ignored


def test_dir_cache_from_before_the_store_is_rebuilt(tmp_path: Path, listing: str):
    """A `dir-stats.parquet` written by the dir-only writer (no `mtime_max` /
    `created_max`) is a miss: the rollup is recomputed from the listing and
    the cache rewritten with the store's columns."""
    from dt_cloud.viz import DIR_STATS_COLS

    cache = tmp_path / "dir-cache"
    write_path_index((listing,), tmp_path / "cold", "2026-07-20", dir_cache=cache)
    old = pd.read_parquet(cache / "dir-stats.parquet").drop(columns=["mtime_max", "created_max"])
    old.to_parquet(cache / "dir-stats.parquet", index=False)
    pidx = tmp_path / "idx" / "path-index.parquet"
    write_path_index((listing,), tmp_path / "warm", "2026-07-20", dir_cache=cache, path_index=pidx)
    assert list(pd.read_parquet(cache / "dir-stats.parquet").columns) == list(DIR_STATS_COLS)
    assert _rows(pd.read_parquet(pidx), ["path", "mtime"])[:1] == [("b1", int(TS["d0703"].timestamp()))]


def test_store_without_user_sorts(tmp_path: Path, listing: str, attribution: str):
    """`user_sorts=False` writes the two sorts only, attributed rows and all —
    the reader prunes a lens by the footer's `u_min`/`u_max` (gcs's shape:
    half the bytes and footer rows; specs/path-store.md §1.6)."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    out = tmp_path / "out"
    pidx = tmp_path / "path-index.parquet"
    meta = write_path_index((listing,), out, "2026-07-20", (attribution,), identities_path, path_index=pidx, user_sorts=False, row_group_rows=2048)
    assert meta["index"] == {"rows": 12, "sorts": {"path": {"rows": 12, "groups": 1}, "bysize": {"rows": 12, "groups": 1}}}
    assert sorted(p.name for p in tmp_path.glob("path-index*")) == [
        "path-index-bysize.groups.json", "path-index-bysize.groups.parquet", "path-index-bysize.parquet",
        "path-index.groups.json", "path-index.groups.parquet", "path-index.parquet",
    ]
    assert _kv(pidx) == {"tier": "path", "sort": "depth,path,usr"}



def test_filesystem_root_capture_splits_on_first_segment(tmp_path: Path):
    """A `capture /`'s rows all carry bucket `/` (the scan root). Its top-level
    dirs become the depth-1 roots (`Applications`, `Users`), as `Users/ryan`'s
    first segment does for a home capture — not one '' root with `/`-led
    children, whose parent walk never terminates. Files directly under `/`
    (`.file`, `.VolumeIcon.icns`) have no root to sit in and are dropped."""
    listing_path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["/"] * 3,
            "name": ["Users/ryan/a.bin", "Applications/X.app/b", ".file"],
            "size_bytes": [2 * GB, 1 * GB, 0],
            "created": [TS["d0701"]] * 3,
            "storage_class_id": [1] * 3,
        }
    ).to_parquet(listing_path)
    pidx = tmp_path / "idx" / "path-index.parquet"
    write_path_index((str(listing_path),), tmp_path / "out", "2026-07-20", path_index=pidx)
    df = pd.read_parquet(pidx)
    assert _rows(df, ["path", "kind", "size"]) == [
        ("Applications", "dir", 1 * GB), ("Users", "dir", 2 * GB),
        ("Applications/X.app", "dir", 1 * GB), ("Users/ryan", "dir", 2 * GB),
        ("Applications/X.app/b", "file", 1 * GB), ("Users/ryan/a.bin", "file", 2 * GB),
    ]


def test_home_capture_object_depth(tmp_path: Path):
    """A capture of a nested root (`/Users/ryan`) has a multi-segment bucket
    (`Users/ryan`): an object's depth counts the bucket's segments too, so a
    file sits one level below its dir (the `(depth, path)` range a drill reads)."""
    listing_path = tmp_path / "listing.parquet"
    pd.DataFrame(
        {
            "bucket": ["/Users/ryan"] * 2,
            "name": ["top.txt", "c/a.bin"],
            "size_bytes": [1 * GB, 2 * GB],
            "created": [TS["d0701"]] * 2,
            "storage_class_id": [1] * 2,
        }
    ).to_parquet(listing_path)
    pidx = tmp_path / "idx" / "path-index.parquet"
    write_path_index((str(listing_path),), tmp_path / "out", "2026-07-20", path_index=pidx)
    df = pd.read_parquet(pidx)
    assert _rows(df, ["path", "kind", "depth"]) == [
        ("Users", "dir", 1), ("Users/ryan", "dir", 2),
        ("Users/ryan/c", "dir", 3), ("Users/ryan/top.txt", "file", 3),
        ("Users/ryan/c/a.bin", "file", 4),
    ]


def test_rows_carry_bytes_by_age(tmp_path: Path):
    """Every row carries `age_b0`..`age_b6`: bytes by age at the scan date in
    log buckets (<1d, <1w, <1mo, <3mo, <1y, <3y, older), rolled up like
    `size` — a dir's buckets sum its subtree's objects, a file's one bucket
    is its own size. The mean alone can't tell `.cargo`'s 2006-stamped
    sources from its recent files."""
    listing_path = tmp_path / "listing.parquet"
    ts = lambda s: pd.Timestamp(s, tz="UTC")  # noqa: E731
    pd.DataFrame(
        {
            "bucket": ["b1"] * 7,
            "name": ["new/a", "new/b", "new/c", "mid/d", "mid/e", "old/f", "old/g"],
            "size_bytes": [1, 2, 4, 8, 16, 32, 64],
            "created": [ts("2026-07-20 03:00"), ts("2026-07-15"), ts("2026-07-01"), ts("2026-05-01"),
                        ts("2026-01-01"), ts("2024-07-20"), ts("2006-07-24")],
            "storage_class_id": [1] * 7,
        }
    ).to_parquet(listing_path)
    pidx = tmp_path / "idx" / "path-index.parquet"
    write_path_index((str(listing_path),), tmp_path / "out", "2026-07-20", path_index=pidx, age_strata=True)
    df = pd.read_parquet(pidx)
    ages = [f"age_b{i}" for i in range(7)]
    assert list(df.columns) == STORE_COLS[:12] + ages + STORE_COLS[12:]
    assert _rows(df, ["path", *ages]) == [
        ("b1", 1, 2, 4, 8, 16, 32, 64),
        ("b1/mid", 0, 0, 0, 8, 16, 0, 0), ("b1/new", 1, 2, 4, 0, 0, 0, 0), ("b1/old", 0, 0, 0, 0, 0, 32, 64),
        ("b1/mid/d", 0, 0, 0, 8, 0, 0, 0), ("b1/mid/e", 0, 0, 0, 0, 16, 0, 0),
        ("b1/new/a", 1, 0, 0, 0, 0, 0, 0), ("b1/new/b", 0, 2, 0, 0, 0, 0, 0), ("b1/new/c", 0, 0, 4, 0, 0, 0, 0),
        ("b1/old/f", 0, 0, 0, 0, 0, 32, 0), ("b1/old/g", 0, 0, 0, 0, 0, 0, 64),
    ]


def test_access_aggregates_mix_day_and_hour_grain(tmp_path: Path, listing: str, attribution: str):
    """The access aggregates' grain moved from `day` to `hour`; a glob spans
    both shapes until the day-grain parts age out (gcs 2026-10-01: `path-index`
    died on "schema mismatch in glob: column day"). Parts are read by name and
    the day taken from whichever column each has — the as-of rule included."""
    parts = tmp_path / "agg"
    parts.mkdir()
    pd.DataFrame({
        "bucket": ["b1", "b1"], "path": ["users/rw/ckpt", "users"],
        "day": [TS["d0703"].date(), TS["d0703"].date()], "op": ["GET", "GET"],
        "last_ts": [TS["d0703"], TS["d0703"]], "n_ops": [3, 3], "bytes_out": [1000, 1000],
    }).to_parquet(parts / "a-day.parquet")
    pd.DataFrame({
        "bucket": ["b1", "b1"], "path": ["datasets/raw", "datasets"],
        "hour": [TS["d0702"], TS["d0720"]], "op": ["GET", "GET"],
        "last_ts": [TS["d0702"], TS["d0720"]], "n_ops": [1, 9], "bytes_out": [10, 9000],
    }).to_parquet(parts / "b-hour.parquet")
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    pidx = tmp_path / "path-index.parquet"
    meta = write_path_index((listing,), tmp_path / "out", "2026-07-20", (attribution,), identities_path, access=(str(parts / "*.parquet"),), path_index=pidx)
    assert meta["access"] == {"from": epoch_day(TS["d0702"]), "to": epoch_day(TS["d0703"])}
    a_by_path = pd.read_parquet(pidx).groupby("path")["last_read"].max().to_dict()
    assert (a_by_path["b1/users/rw/ckpt"], a_by_path["b1/datasets/raw"], a_by_path["b1/datasets"], a_by_path["b1"]) == (
        epoch_day(TS["d0703"]), epoch_day(TS["d0702"]), epoch_day(TS["d0702"]), epoch_day(TS["d0703"]),
    )


def test_store_with_only_the_bysize_user_sort(tmp_path: Path, listing: str, attribution: str):
    """`user_sort_tiers=("bysize",)` writes the two sorts plus one user-first
    copy, `bysize` by `usr` — what a lens view's root reads (gcs writes this
    shape: `path-index -u bysize`)."""
    identities_path = tmp_path / "identities.yaml"
    identities_path.write_text(IDENTITIES_YAML)
    pidx = tmp_path / "path-index.parquet"
    meta = write_path_index((listing,), tmp_path / "out", "2026-07-20", (attribution,), identities_path, path_index=pidx, user_sort_tiers=("bysize",))
    assert meta["index"] == {"rows": 12, "sorts": {"path": {"rows": 12, "groups": 1}, "bysize": {"rows": 12, "groups": 1}, "bysize-user": {"rows": 12, "groups": 1}}}
    assert sorted(p.name for p in tmp_path.glob("path-index*.parquet")) == [
        "path-index-bysize-by-user.groups.parquet", "path-index-bysize-by-user.parquet", "path-index-bysize.groups.parquet",
        "path-index-bysize.parquet", "path-index.groups.parquet", "path-index.parquet",
    ]
    assert _kv(pidx.with_name("path-index-bysize-by-user.parquet")) == {"tier": "bysize", "sort": "usr,size_bucket desc,path", "bucket": "log2"}
