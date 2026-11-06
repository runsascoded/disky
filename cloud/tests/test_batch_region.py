"""A Batch job's region: named by the deployment (`$BATCH_REGION`, or explicitly), never defaulted, and checked against
the buckets the job mounts or names before `batch.submit_job` posts it (`batch.check_region`)."""

import json

import pytest
from click.testing import CliRunner

from dt_cloud import batch, gcp
from dt_cloud.batch import CrossRegion, check_region, job_regions, mounted_buckets, submit_job
from dt_cloud.cli import main
from dt_cloud.deploy import batch_region
from dt_cloud.scan_runs import Profile, lookup_job

LABELS = {"app": "disky", "deployment": "test", "component": "race"}
NO_REGION = "export BATCH_REGION (the region the deployment's Batch jobs run in, beside its buckets)"
OVERRIDE = "Run it beside them, or pass -X/--cross-region (or export BATCH_CROSS_REGION=1) to submit it and pay for the transfer"
FIELDS = {"fields": "location,locationType,customPlacementConfig"}

EAST1 = {"location": "US-EAST1", "locationType": "region"}
US = {"location": "US", "locationType": "multi-region"}
EU = {"location": "EU", "locationType": "multi-region"}
NAM4 = {"location": "NAM4", "locationType": "dual-region"}
PLACED = {"location": "US", "locationType": "dual-region", "customPlacementConfig": {"dataLocations": ["US-EAST1", "US-WEST1"]}}


def job(*mounts: str, allowed: list[str] | None = None) -> dict:
    """A labelled Batch spec mounting `mounts` (gcsfuse `remotePath`s), its VMs in `allowed` when given."""
    spec = {
        "labels": LABELS,
        "allocationPolicy": {"labels": LABELS, **({"location": {"allowedLocations": allowed}} if allowed else {})},
        "taskGroups": [{"taskSpec": {"volumes": [{"gcs": {"remotePath": m}, "mountPath": f"/mnt/disks/gcs/{m.split('/')[0]}"} for m in mounts]
                                     + [{"deviceName": "stage", "mountPath": "/mnt/disks/stage"}]}}],
    }
    return spec


class Response:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code, self.body = status_code, body

    def json(self):
        return self.body

    def raise_for_status(self):
        assert self.status_code == 200


class Net:
    """`batch.session()`: `buckets` maps a bucket to its `buckets.get` body, or to the status that refuses it."""

    def __init__(self, buckets: dict[str, dict | int]):
        self.buckets, self.gets, self.posts = buckets, [], []

    def __call__(self):
        return self

    def get(self, url, params=None):
        self.gets.append((url, params))
        meta = self.buckets[url.rsplit("/", 1)[-1]]
        return Response(meta) if isinstance(meta, int) else Response(200, meta)

    def post(self, url, json, params):
        self.posts.append((url, params))
        return Response(200, {"name": f"projects/p/locations/r/jobs/{params['job_id']}"})


@pytest.fixture
def net(monkeypatch):
    """A fake network, and no bucket location remembered from another test."""
    n = Net({"east": EAST1, "us": US, "eu": EU, "nam4": NAM4, "placed": PLACED, "secret": 403, "gone": 404,
             "new": {"location": "MARS1", "locationType": "dual-region"}})
    monkeypatch.setattr(batch, "session", n)
    monkeypatch.setattr(batch, "_LOCATIONS", {})
    return n


def bucket_gets(*buckets: str) -> list[tuple]:
    return [(f"https://storage.googleapis.com/storage/v1/b/{b}", FIELDS) for b in buckets]


def post(region: str, job_id: str) -> tuple:
    return (f"https://batch.googleapis.com/v1/projects/p/locations/{region}/jobs", {"job_id": job_id})


# ── the region: explicit, else $BATCH_REGION, else an error naming both ──────────────────────────────────────────────

def test_batch_region_is_explicit_then_the_env(monkeypatch):
    assert (batch_region(), batch_region(" us-east5 "), batch_region(None, "pass -L/--region")) == ("my-region", "us-east5", "my-region")
    monkeypatch.setenv("BATCH_REGION", " europe-west4 ")
    assert (batch_region(), batch_region("us-east5")) == ("europe-west4", "us-east5")


@pytest.mark.parametrize("value", [None, "", "  "])
def test_no_region_is_an_error_naming_both(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("BATCH_REGION")
    else:
        monkeypatch.setenv("BATCH_REGION", value)
    for how, message in ((None, f"no Batch region: {NO_REGION}"), ("pass -L/--region", f"no Batch region: pass -L/--region, or {NO_REGION}")):
        with pytest.raises(SystemExit) as e:
            batch_region(None, how)
        assert str(e.value) == message


def test_every_reader_of_the_old_default_needs_a_region(monkeypatch, net):
    """Nothing falls back to a region of dt-cloud's own: each helper that took the old default refuses, before the network."""
    monkeypatch.delenv("BATCH_REGION")
    monkeypatch.setenv("JOB_SA", "job@p")
    monkeypatch.setenv("JOB_IMAGE", "img")
    monkeypatch.setattr(gcp, "session", lambda: pytest.fail("reached the network"))
    calls = {
        "batch_jobs": lambda: gcp.batch_jobs("p"),
        "batch_job": lambda: gcp.batch_job("j", "p"),
        "batch_job_by_uid": lambda: gcp.batch_job_by_uid("u", "p"),
        "submit_job": lambda: submit_job(job(), "j", project="p"),
        "cancel_labelled": lambda: batch.cancel_labelled({"k": "v"}),
        "listing_job_spec": lambda: batch.listing_job_spec("2026-10-09", ["b1"]),
    }
    said = {}
    for name, call in calls.items():
        with pytest.raises(SystemExit) as e:
            call()
        said[name] = str(e.value)
    assert said == dict.fromkeys(calls, f"no Batch region: {NO_REGION}")
    assert (net.gets, net.posts) == ([], [])


def test_an_explicit_region_needs_no_env(monkeypatch, net):
    monkeypatch.delenv("BATCH_REGION")
    assert submit_job(job("east"), "j", "us-east1", "p") == "j"
    assert net.posts == [post("us-east1", "j")]


@pytest.mark.parametrize("args, how", [
    (["batch", "submit", "-c", "race", "-p", "p", "j", "-"], "pass -L/--region"),
    (["batch-cancel", "-l", "k=v"], "pass -r/--region"),
    (["job", "submit-listing", "-d", "2026-10-09", "-b", "b1"], "name b1's region in $LISTING_REGIONS"),
])
def test_cli_without_a_region_says_how_to_name_one(monkeypatch, net, args, how):
    monkeypatch.delenv("BATCH_REGION")
    r = CliRunner().invoke(main, args, input=json.dumps(job()))
    assert (r.exit_code, r.output) == (1, f"no Batch region: {how}, or {NO_REGION}\n")
    assert (net.gets, net.posts) == ([], [])


def test_submit_listing_takes_each_buckets_region_from_its_own_config(monkeypatch):
    """`$LISTING_REGIONS` names a bucket's region; only a bucket it doesn't name needs `$BATCH_REGION`."""
    calls = []
    monkeypatch.setattr(batch, "listing_job_spec", lambda date, buckets, **kw: {"buckets": buckets, "region": kw["region"]})
    monkeypatch.setattr(batch, "submit_job", lambda spec, region, cross_region: calls.append((spec, region, cross_region)) or f"job-{region}")
    monkeypatch.setenv("LISTING_REGIONS", '{"b1": "europe-west4", "b2": "us-east5"}')
    monkeypatch.delenv("BATCH_REGION")
    r = CliRunner().invoke(main, ["job", "submit-listing", "-d", "2026-10-09", "-b", "b1", "-b", "b2"])
    assert (r.exit_code, r.stdout) == (0, "job-europe-west4\njob-us-east5\n")
    monkeypatch.setenv("BATCH_REGION", "us-east1")
    r = CliRunner().invoke(main, ["job", "submit-listing", "-X", "-d", "2026-10-09", "-b", "b1", "-b", "b3", "-b", "b4"])
    assert (r.exit_code, r.stdout) == (0, "job-europe-west4\njob-us-east1\n")
    assert calls == [
        ({"buckets": ["b1"], "region": "europe-west4"}, "europe-west4", None),
        ({"buckets": ["b2"], "region": "us-east5"}, "us-east5", None),
        ({"buckets": ["b1"], "region": "europe-west4"}, "europe-west4", True),
        ({"buckets": ["b3", "b4"], "region": "us-east1"}, "us-east1", True),
    ]


@pytest.mark.parametrize("regions, region, env, looked", [
    (("us-east5", "us-east1"), None, None, "us-east5"),
    (("us-east5",), "us-west4", "us-east1", "us-west4"),
    ((), None, "us-east1", "us-east1"),
])
def test_scan_run_looks_its_job_up_in_its_profiles_region(monkeypatch, regions, region, env, looked):
    seen = []
    monkeypatch.setattr(gcp, "batch_job", lambda name, project, region: seen.append((name, project, region)) or {"name": name})
    if env is None:
        monkeypatch.delenv("BATCH_REGION")
    else:
        monkeypatch.setenv("BATCH_REGION", env)
    profile = Profile(store="s", project="p", regions=regions)
    assert lookup_job(profile, "run", "job-1", False, region) == {"name": "job-1"}
    assert seen == [("job-1", "p", looked)]


def test_scan_run_lookup_without_any_region_is_an_error(monkeypatch):
    monkeypatch.delenv("BATCH_REGION")
    with pytest.raises(SystemExit) as e:
        lookup_job(Profile(store="s", project="p"), "run", "job-1", False, None)
    assert str(e.value) == f"no Batch region: pass -R/--region, name the profile's `regions`, or {NO_REGION}"


# ── the guard: a job runs in the region of the buckets it mounts or names ────────────────────────────────────────────

def test_job_regions_and_mounts():
    assert job_regions(job(), "us-east1") == ["us-east1"]
    assert job_regions(job(allowed=["regions/us-east5"]), "us-east1") == ["us-east5"]
    assert job_regions(job(allowed=["zones/us-central1-a", "zones/us-central1-f", "regions/europe-west4"]), "us-east1") == ["us-central1", "europe-west4"]
    assert mounted_buckets(job("east", "us/sub/dir", "east/other")) == ["east", "us"]
    assert mounted_buckets({"taskGroups": [{}, {"taskSpec": {}}]}) == []


def test_a_mismatched_region_is_refused_before_the_post(net):
    with pytest.raises(CrossRegion) as e:
        submit_job(job("east", "us"), "race-1", "us-central1", "p")
    assert str(e.value) == f"Batch job race-1 in us-central1: not in the region of its bucket(s) east (US-EAST1). {OVERRIDE}"
    assert (net.gets, net.posts) == (bucket_gets("east", "us"), [])


def test_the_vms_region_is_what_is_checked_not_the_jobs(net):
    """`allowedLocations` places the VMs: a job resource in the bucket's region whose VMs run elsewhere is refused, and
    each mismatched bucket is named."""
    with pytest.raises(CrossRegion) as e:
        submit_job(job("east", "eu", "placed", allowed=["zones/us-central1-a"]), "race-1", "us-east1", "p")
    assert str(e.value) == f"Batch job race-1 in us-central1: not in the region of its bucket(s) east (US-EAST1), eu (EU), placed (US-EAST1+US-WEST1). {OVERRIDE}"
    assert submit_job(job("east", allowed=["regions/us-east1"]), "race-2", "us-central1", "p") == "race-2"
    assert net.posts == [post("us-central1", "race-2")]


@pytest.mark.parametrize("how", ["argument", "env"])
def test_the_override_submits_without_looking(monkeypatch, net, how):
    if how == "env":
        monkeypatch.setenv("BATCH_CROSS_REGION", "1")
    assert submit_job(job("east"), "race-1", "us-central1", "p", **({"cross_region": True} if how == "argument" else {})) == "race-1"
    assert (net.gets, net.posts) == ([], [post("us-central1", "race-1")])


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_the_override_env_is_exactly_1(monkeypatch, net, value):
    if value is not None:
        monkeypatch.setenv("BATCH_CROSS_REGION", value)
    with pytest.raises(CrossRegion):
        submit_job(job("east"), "race-1", "us-central1", "p")
    with pytest.raises(CrossRegion):  # an explicit False is not overridden by the env
        monkeypatch.setenv("BATCH_CROSS_REGION", "1")
        submit_job(job("east"), "race-1", "us-central1", "p", cross_region=False)
    assert net.posts == []


def test_a_matching_region_passes_with_one_get_per_bucket_per_process(net, capsys):
    assert submit_job(job("east", "east/sub"), "a", "us-east1", "p") == "a"
    assert submit_job(job("east", "nam4"), "b", "us-east1", "p") == "b"
    assert (net.gets, net.posts) == (bucket_gets("east", "nam4"), [post("us-east1", "a"), post("us-east1", "b")])
    assert capsys.readouterr().err == ""


def test_an_unreadable_bucket_is_noted_and_not_checked(net, capsys):
    assert submit_job(job("secret", "east", "gone"), "race-1", "us-east1", "p") == "race-1"
    assert capsys.readouterr().err.splitlines() == [
        "note: Batch job race-1: bucket secret: location unreadable (HTTP 403); its region is not checked",
        "note: Batch job race-1: bucket gone: location unreadable (HTTP 404); its region is not checked",
    ]
    assert submit_job(job("secret"), "race-2", "us-east1", "p") == "race-2"  # remembered: not asked again
    assert (net.gets, net.posts) == (bucket_gets("secret", "east", "gone"), [post("us-east1", "race-1"), post("us-east1", "race-2")])


def test_an_unreadable_bucket_does_not_excuse_a_readable_one(net):
    with pytest.raises(CrossRegion) as e:
        submit_job(job("secret", "east"), "race-1", "us-central1", "p")
    assert str(e.value) == f"Batch job race-1 in us-central1: not in the region of its bucket(s) east (US-EAST1). {OVERRIDE}"


@pytest.mark.parametrize("bucket, inside, outside", [
    ("east", ["us-east1"], ["us-east4", "us-central1", "europe-west4"]),
    ("us", ["us-east1", "us-central1", "us-west4"], ["europe-west4", "northamerica-northeast1", "asia-east1"]),
    ("eu", ["europe-west4", "europe-north1", "europe-west2"], ["us-east1", "asia-east1"]),
    ("nam4", ["us-central1", "us-east1"], ["us-east4", "us-west1"]),
    ("placed", ["us-east1", "us-west1"], ["us-central1"]),
])
def test_multi_and_dual_region_buckets_match_the_regions_inside_them(net, bucket, inside, outside):
    assert [check_region(job(bucket), r) for r in inside] == [[] for _ in inside]
    refused = []
    for r in outside:
        with pytest.raises(CrossRegion):
            check_region(job(bucket), r)
        refused.append(r)
    assert (refused, net.gets) == (outside, bucket_gets(bucket))


def test_a_location_code_the_check_does_not_know_is_noted(net):
    assert check_region(job("new"), "us-east1", what="Batch job j") == [
        "Batch job j: bucket new: location MARS1 (dual-region) is not one this check knows; its region is not checked",
    ]


def test_a_named_bucket_is_checked_like_a_mounted_one(net):
    with pytest.raises(CrossRegion) as e:
        submit_job(job("us"), "race-1", "us-central1", "p", buckets=["east", "us"])
    assert str(e.value) == f"Batch job race-1 in us-central1: not in the region of its bucket(s) east (US-EAST1). {OVERRIDE}"
    assert net.gets == bucket_gets("us", "east")


def test_a_job_with_no_buckets_is_not_checked(net):
    assert submit_job(job(), "race-1", "us-central1", "p") == "race-1"
    assert (net.gets, net.posts) == ([], [post("us-central1", "race-1")])


def _cli(tmp_path, *args: str, mounts: tuple[str, ...] = ("east",)):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"taskGroups": job(*mounts)["taskGroups"]}))
    return CliRunner().invoke(main, ["batch", "submit", "-c", "race", "-p", "p", *args, "race-1", str(spec)])


def test_cli_refuses_a_mismatched_region(net, tmp_path):
    r = _cli(tmp_path, "-L", "us-central1")
    assert (r.exit_code, type(r.exception), str(r.exception), r.output) == (
        1, CrossRegion, f"Batch job race-1 in us-central1: not in the region of its bucket(s) east (US-EAST1). {OVERRIDE}", "")
    assert net.posts == []


def test_cli_override_and_matching_region_pass(net, tmp_path, monkeypatch):
    monkeypatch.setenv("BATCH_REGION", "us-east1")
    results = [_cli(tmp_path, "-L", "us-central1", "-X"), _cli(tmp_path, "-L", "us-central1", "--cross-region"), _cli(tmp_path)]
    assert [(r.exit_code, r.output) for r in results] == [(0, "race-1\n")] * 3
    assert (net.gets, net.posts) == (bucket_gets("east"), [post("us-central1", "race-1"), post("us-central1", "race-1"), post("us-east1", "race-1")])


def test_cli_names_buckets_the_job_reaches_by_url(net, tmp_path):
    r = _cli(tmp_path, "-L", "us-east1", "-b", "eu", "-b", "east", mounts=())
    assert (r.exit_code, type(r.exception), str(r.exception)) == (
        1, CrossRegion, f"Batch job race-1 in us-east1: not in the region of its bucket(s) eu (EU). {OVERRIDE}")
    assert net.gets == bucket_gets("eu", "east")


def test_cli_notes_an_unreadable_bucket_on_stderr(net, tmp_path):
    r = _cli(tmp_path, "-L", "us-east1", mounts=("secret",))
    assert (r.exit_code, r.stdout, r.stderr) == (
        0, "race-1\n", "note: Batch job race-1: bucket secret: location unreadable (HTTP 403); its region is not checked\n")
