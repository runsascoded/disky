"""The scan profile (specs/pipelines-everywhere.md §3): validated on load, every problem named by its key path."""
from __future__ import annotations

from pathlib import Path

import pytest

from dt_cloud.scan_profile import Disk, Machine, ProfileError, Source, load, parse

FIXTURE = Path(__file__).parent / "fixtures" / "scan_profiles" / "cw-s3.yml"


def minimal(**kw) -> dict:
    d = {"deployment": "fix", "executor": {"kind": "local"}, "work": "/w", "data": "/d",
         "layout": {"layer2": "l2/{scan}/", "snapshots": ""}, "sources": ["s3://a"],
         "options": {"list": {"import": True}, "static-names": {"profile": "s.json"}, "digest": {"template": "t", "config": "c"}}}
    d.update(kw)
    return d


def problems(d: dict) -> list[str]:
    with pytest.raises(ProfileError) as e:
        parse(d)
    return e.value.problems


def test_fixture_loads():
    p = load(FIXTURE)
    assert p.enabled() == ("bucket-check", "list", "index", "static-names", "publish-r2", "interval-store", "index-rows",
                           "index-sync", "over-time", "index-gc", "warm-cache", "probe", "digest")
    assert p.sources == (
        Source("s3://bucket-big", machine="n2-standard-8", procs=8),
        Source("s3://bucket-b"),
        Source("s3://bucket-c", disk=Disk(375, "local-ssd")),
    )
    assert [(s.kind, s.bucket) for s in p.sources] == [("s3", "bucket-big"), ("s3", "bucket-b"), ("s3", "bucket-c")]
    assert p.machines["big"] == Machine("n2-standard-8", Disk(200))
    assert p.fold == ("bucket-check", "index-sync")
    assert p.data_bucket == "example-data"


def test_required_keys_named():
    assert problems({"deployment": "x"}) == [
        "executor: required", "work: required", "data: required", "layout: required", "sources: required",
    ]


def test_unknown_keys_refused():
    d = minimal(colour="red", executor={"kind": "local", "zone": "z"}, sources=[{"uri": "s3://a", "size": 1}],
                options={"list": {"import": True, "turbo": True}, "static-names": {"profile": "s"},
                         "digest": {"template": "t", "config": "c"}, "nope": {}})
    assert problems(d) == [
        "colour: unknown key",
        "executor.zone: unknown key",
        "sources[0].size: unknown key",
        "options.list.turbo: unknown key (known: import, bulk_args, import_args, threads, weights, secrets)",
        "options.nope: unknown step",
    ]


def test_google_batch_requirements():
    d = minimal(executor={"kind": "google-batch", "project": "p"}, work="gs://w/nf", data="gs://other")
    assert problems(d) == [
        "executor.region: required for google-batch",
        "executor.image: required for google-batch",
        "data: must be in work's bucket (w): google-batch tasks mount only the work dir's bucket",
    ]


def test_local_executor_needs_a_local_work_dir():
    assert problems(minimal(work="gs://w/nf")) == [
        "work: a local dir for the local executor (it refuses a cloud work dir without Fusion)",
    ]


def test_sources_and_disks():
    d = minimal(sources=[{"uri": "ftp://x"}, {"uri": "s3://a", "disk": {"request": 400, "type": "local-ssd"}, "procs": 0},
                         "s3://a", {"uri": "gs://g", "region": "europe-west4"}],
                machines={"big": {"type": "n2", "disk": {"request": 10, "type": "nvme"}}})
    assert problems(d) == [
        "sources[0].uri: <scheme>://<bucket> with scheme one of gs, gcs, s3, r2, hf (got 'ftp://x')",
        "sources[1].procs: a positive integer",
        "sources[1].disk.request: a multiple of 375 for local-ssd (got 400)",
        "sources[2].uri: bucket 'a' listed twice",
        "sources[3].region: 'europe-west4' ≠ the executor's (None); nf-google 1.29.1 has no per-task location (spec §4 'Per-source region')",
        "machines.big.disk.type: one of local-ssd, pd-balanced, pd-ssd, pd-standard",
    ]


def test_steps_skip_fold():
    d = minimal(steps=["index", "warp"], skip=["list", "probe"], fold=["index"])
    assert problems(d) == [
        "steps: 'index' is a core step (always on unless in `skip`)",
        "steps: unknown step 'warp'",
        "skip: 'list' can't be skipped (one of bucket-check, static-names, publish-r2, interval-store, over-time, index-gc, warm-cache, probe, digest)",
        "fold: 'index' can't be folded (one of bucket-check, index-sync)",
    ]


def test_skip_and_fold_apply():
    p = parse(minimal(skip=["probe", "digest", "static-names"], fold=[], options={"list": {"import": True}}))
    assert (p.enabled(), p.fold) == (
        ("bucket-check", "list", "index", "publish-r2", "interval-store", "index-rows", "index-sync", "over-time", "index-gc", "warm-cache"),
        (),
    )


def test_step_requirements():
    d = minimal(layout={"layer2": "l2/", "snapshots": None}, options={"index": {"mode": "path-index", "access": "a"}})
    d["data"] = "s3://dvx"
    assert problems(d) == [
        "layout.layer2: a template with `{scan}`",
        "layout.snapshots: required (\"\" for `snapshots/` itself)",
        "layout.listing: required unless options.list.import is true (index reads the listings from the data store)",
        "data: a gs:// bucket or a local dir with options.index.mode path-index (its inputs are staged from GCS or read in place)",
        "options.static-names.profile: required (the deployment's static-names profile; or env STATIC_NAMES_PROFILE, or gen: the one-machine path, or skip: [static-names])",
        "options.digest.template: required (or skip: [digest])",
        "options.digest.config: required (or skip: [digest])",
    ]


def test_option_types():
    d = minimal(options={"list": {"import": "yes", "bulk_args": "-a"}, "static-names": {"profile": "s"},
                         "digest": {"template": "t", "config": "c"}, "index-gc": {"grace": 2}})
    assert problems(d) == [
        "options.list.import: true or false",
        "options.list.bulk_args: a list of strings",
        "options.index-gc.grace: a string",
    ]


def test_machine_labels_are_groovy_identifiers():
    """A label goes bare into the executor config (`withLabel: over_time { … }`): `_` yes, `-` never."""
    assert sorted(parse(minimal(machines={"over_time": "n2-standard-8", "post": "e2-highmem-4"})).machines) == ["over_time", "post"]
    assert problems(minimal(machines={"over-time": "n2-standard-8", "Big": "n2-standard-8"})) == [
        "machines.over-time: a label of lowercase letters, digits and '_' (it is written bare into the Nextflow config)",
        "machines.Big: a label of lowercase letters, digits and '_' (it is written bare into the Nextflow config)",
    ]


GB = {"kind": "google-batch", "project": "p", "region": "r", "image": "i"}


def gb(**machines) -> dict:
    return minimal(executor=GB, work="gs://w/nf", data="gs://w", machines=machines)


def test_machine_fallback():
    """A label's `fallback` types + `queue_timeout` (spec §7): the types by submit attempt, the primary first."""
    big = {"type": "n2-highmem-32", "disk": {"request": 1500, "type": "local-ssd"}, "fallback": ["n2d-highmem-32"], "queue_timeout": 10}
    m = parse(gb(big=big)).machines["big"]
    assert m == Machine("n2-highmem-32", Disk(1500, "local-ssd"), fallback=("n2d-highmem-32",), queue_timeout=10)
    assert m.types == ("n2-highmem-32", "n2d-highmem-32")
    assert parse(gb(big="n2-highmem-32")).machines["big"].types == ("n2-highmem-32",)


def test_machine_fallback_refused():
    assert problems(gb(
        a={"type": "t", "fallback": ["u"]},
        b={"type": "t", "queue_timeout": 10},
        c={"type": "t", "fallback": [], "queue_timeout": 0},
        d={"fallback": ["u"], "queue_timeout": 5},
        e={"type": "t", "fallback": ["u", "t"], "queue_timeout": 5},
        f={"type": "t", "fallback": "u", "queue_timeout": "10m"},
        list={"type": "t", "fallback": ["u"], "queue_timeout": 5},
    )) == [
        "machines.a.fallback: needs `queue_timeout` (a queued task never fails, so nothing else moves it on)",
        "machines.b.queue_timeout: only with `fallback` (a resubmit of the same type waits in the same pool)",
        "machines.c.fallback: a non-empty list of machine types",
        "machines.c.queue_timeout: a positive number of minutes",
        "machines.d.fallback: needs the primary `type`",
        "machines.e.fallback: each machine type once (with `type`)",
        "machines.f.fallback: a list of strings",
        "machines.f.queue_timeout: a positive number of minutes",
        "machines.list.fallback: not on `list` (each source's machine is its own)",
    ]
    assert problems(minimal(machines={"big": {"type": "t", "fallback": ["u"], "queue_timeout": 5}})) == [
        "machines.big.fallback: google-batch only (the only executor whose tasks take a machine type)",
    ]


META = Path(__file__).parent / "fixtures" / "scan_profiles" / "meta-store.yml"


def test_store():
    """`store` (default the primary) names the site store; a store other than the primary runs only the steps that can
    serve it (no interval store, warm-up or probe: they read the primary's)."""
    assert (parse(minimal()).store, parse(minimal()).primary) == ("primary", True)
    p = load(META)
    assert (p.store, p.primary, p.enabled()) == ("meta", False, ("list", "index", "static-names", "publish-r2", "index-rows", "index-sync", "index-gc"))
    assert problems(minimal(store="Meta")) == ["store: a `STORES_JSON` key: lowercase letters, digits and '-' (got 'Meta')"]
    assert problems(minimal(store="meta")) == [
        "store meta: skip interval-store (the site serves another store's scans per scan, and the interval store's "
        "`interval-store/CURRENT.json` is the primary's)",
        "store meta: skip warm-cache (its requests carry no `store=`: it would warm the primary's views)",
        "store meta: skip probe (its requests carry no `store=`: it would probe the primary's views)",
    ]


def test_secrets_overlay():
    """Each call's bindings: the profile's, then `options.<step>.secrets`, then (a list call) its source's; the most
    specific binding of an ENV wins."""
    p = load(META)
    assert p.sources[1] == Source("r2://example-serving", endpoint="https://example-account.r2.example.org",
                                  secrets={"AWS_ACCESS_KEY_ID": "example-serving-key-id", "AWS_SECRET_ACCESS_KEY": "example-serving-secret"})
    base = {"CLOUDFLARE_API_TOKEN": "example-cf-token", "R2_ACCESS_KEY_ID": "example-serving-key-id",
            "R2_SECRET_ACCESS_KEY": "example-serving-secret"}
    assert [p.secrets_for("index"), p.secrets_for("list"), p.secrets_for("list", "gcs://example-data"),
            p.secrets_for("list", "r2://example-serving")] == [
        base,
        {**base, "AWS_ACCESS_KEY_ID": "example-source-key-id", "AWS_SECRET_ACCESS_KEY": "example-source-secret"},
        {**base, "AWS_ACCESS_KEY_ID": "example-source-key-id", "AWS_SECRET_ACCESS_KEY": "example-source-secret"},
        {**base, "AWS_ACCESS_KEY_ID": "example-serving-key-id", "AWS_SECRET_ACCESS_KEY": "example-serving-secret"},
    ]
    assert sorted(p.secret_names()) == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLOUDFLARE_API_TOKEN",
                                        "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]


def test_secrets_refused():
    d = minimal(sources=[{"uri": "s3://a", "secrets": {"bad-name": "s", "OK": ["x"]}}, {"uri": "s3://b", "secrets": "s"}],
                options={"list": {"import": True, "secrets": {"K": {"x": 1}}}, "static-names": {"profile": "s.json", "secrets": []},
                         "digest": {"template": "t", "config": "c"}})
    assert problems(d) == [
        "sources[0].secrets.bad-name: not an env var name",
        "sources[0].secrets.OK: a scalar",
        "sources[1].secrets: a mapping",
        "options.list.secrets.K: a scalar",
        "options.static-names.secrets: a mapping",
    ]


def test_index_extras_options():
    """The path-index extras (`age_pyramids`, `verify`) only under that mode; `index-sync.variants` are index variants;
    `index-gc.dry` a flag."""
    p = load(META)
    assert ({k: p.opts("index")[k] for k in ("age_pyramids", "verify", "mem")}, p.opts("index-gc")) == (
        {"age_pyramids": True, "verify": True, "mem": "12GB"}, {"dry": True})
    d = minimal(options={"list": {"import": True}, "static-names": {"profile": "s.json"}, "digest": {"template": "t", "config": "c"},
                         "index": {"age_pyramids": True, "verify": "yes"}, "index-sync": {"variants": ["path", "nope"]},
                         "index-gc": {"dry": "no"}})
    assert problems(d) == [
        "options.index.verify: true or false",
        "options.index-gc.dry: true or false",
        "options.index.age_pyramids: only with mode path-index (index-write writes the age pyramids itself)",
        "options.index.verify: only with mode path-index",
        "options.index-sync.variants: 'nope' is not an index variant (one of path, bysize, user, bysize-user, age-pyramid-1h, "
        "age-pyramid-3h, age-pyramid-6h, age-pyramid-12h, age-pyramid-1d, age-pyramid-2d, age-pyramid-4d, age-pyramid-8d, over-time)",
    ]
