"""`bulk-list hf://` over recorded Hub API responses (`tests/fixtures/hf/open-athena.json`).

The fixture holds real `open-athena` responses (recorded 2026-10-14 with a
read token; no auth material in them), trimmed: each tree cut to a few
entries, `lastCommit` cut to its date, and the totals the trim invalidates
(`usedStorage`, a bucket's `size`/`totalFiles`) recomputed over what was
kept — plus BFCL-RL's real 2,140 B residual of old versions. biotasks' tree
is split over two pages (a synthetic cursor) to exercise `Link` pagination.
The `lfs-files` and 429 responses are synthetic: the read token gets 403
from `lfs-files`, and the run never hit a 429.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
from pathlib import Path

import pandas as pd
import pytest
from click.testing import CliRunner

from disk_tree.find import bulk_hf
from disk_tree.find.bulk_hf import HfClient, HfError, HfTarget, Response, UnitTotals, parse_hf_uri

FIXTURE = Path(__file__).parent / "fixtures" / "hf" / "open-athena.json"
E = bulk_hf.DEFAULT_ENDPOINT
INFO = "?expand%5B%5D=usedStorage&expand%5B%5D=private&expand%5B%5D=lastModified&expand%5B%5D=sha"
BT = "buckets/biotasks/research/17-notebook2task/2026-10-08/033b3d722c19c8b821c4f1e6789c303591954b7ac384fe5b238d6fee60e42604"
EXP75 = "models/marinfold-exp75"
BFCL = "datasets/BFCL-RL-Campaign-Artifacts-2026.10.11"
CC = "datasets/Complex-Closure-RLVR-Snowball-67B-A2B-Artifacts"


class Replay:
    """A `Transport` serving recorded responses by URL path+query, logging each request."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: "list[tuple[str, dict]]" = []

    def __call__(self, url: str, headers: dict) -> Response:
        self.calls.append((url, headers))
        r = self.responses[url[len(E):]]
        body = r["body"] if isinstance(r["body"], bytes) else json.dumps(r["body"]).encode()
        return Response(r["status"], r["headers"], body)


def client(extra: "dict | None" = None, **kw) -> "tuple[HfClient, Replay]":
    responses = json.loads(FIXTURE.read_text())
    responses.update(extra or {})
    replay = Replay(responses)
    return HfClient(token="t0k", transport=replay, sleep=lambda s: None, **kw), replay


def ok(body, headers=None) -> dict:
    return {"status": 200, "headers": headers or {}, "body": body}


# ---------- URIs ----------

@pytest.mark.parametrize("uri,target", [
    ("hf://open-athena", HfTarget("open-athena")),
    ("hf://Open-Athena/", HfTarget("Open-Athena")),
    ("hf://open-athena/datasets", HfTarget("open-athena", "datasets")),
    ("hf://open-athena/buckets/biotasks", HfTarget("open-athena", "buckets", "biotasks")),
])
def test_parse_hf_uri(uri, target):
    assert parse_hf_uri(uri) == target


def test_target_prefix():
    assert [HfTarget("ns").prefix, HfTarget("ns", "models").prefix, HfTarget("ns", "models", "m").prefix] == ["", "models/", "models/m/"]


@pytest.mark.parametrize("uri", ["hf://", "hf://ns/repos/x", "hf://ns/models/a/b", "gcs://ns"])
def test_parse_hf_uri_rejects(uri):
    with pytest.raises(ValueError):
        parse_hf_uri(uri)


# ---------- listing ----------

EXPECTED_ROWS = [
    (f"{BT}/seta-cytopathology/seed/kernel-metadata.json", 280, "2026-10-08T20:38:43.528Z", "public", None),
    (f"{BT}/bix-asxl1/seed/kernel-metadata.json", 292, "2026-10-08T20:38:45.463Z", "public", None),
    (f"{BT}/bix-asxl1/seed/datasets/data/manifest.json", 538, "2026-10-08T20:38:45.116Z", "public", None),
    (f"{BFCL}/artifacts.tar.gz", 86937622, "2026-10-11T11:49:28.000Z", "public", None),
    (f"{BFCL}/.gitattributes", 2562, "2026-10-11T11:49:28.000Z", "git", None),
    (f"{BFCL}/@history/@unlisted", 2140, "2026-10-12T19:31:09.000Z", "public", None),
    (f"{EXP75}/prot-exp75-cv1-1_5b-e8-lr1e-3-wd0p2-v1-bc3084/checkpoints/step-35679/d/05ae5b225d9a0a196757c8aa5f5ac078", 25169920, "2026-07-19T21:12:05.000Z", "public", None),
    (f"{EXP75}/.gitattributes", 3899, "2026-07-19T21:12:05.000Z", "git", None),
    ("spaces/README/.gitattributes", 1519, "2026-09-02T14:46:52.000Z", "git", None),
    ("spaces/README/README.md", 541, "2026-09-02T14:48:43.000Z", "git", None),
]
EXPECTED_TOTALS = [
    UnitTotals("buckets/biotasks", expected=1110, listed=1110),
    UnitTotals(BFCL, expected=86939762, listed=86937622, git_bytes=2562, residual=2140),
    UnitTotals(EXP75, expected=25169920, listed=25169920, git_bytes=3899),
    UnitTotals("spaces/README", expected=0, listed=0, git_bytes=2060),
]


def test_list_namespace():
    c, replay = client()
    units = list(bulk_hf.list_hf(c, HfTarget("open-athena"), threads=2))
    assert [r for rows, _ in units for r in rows] == EXPECTED_ROWS
    assert [t for _, t in units] == EXPECTED_TOTALS
    # 4 enumerations + bucket tree (2 pages) + 3 × (info + tree)
    assert c.requests == 12
    assert {h["Authorization"] for _, h in replay.calls} == {"Bearer t0k"}


def test_list_namespace_logs_progress(capsys):
    # One line per finished unit (an interval of 0), the last excepted: its totals line follows from the caller.
    c, _ = client()
    list(bulk_hf.list_hf(c, HfTarget("open-athena"), threads=1, progress_s=0))
    assert capsys.readouterr().err.split("\n") == [
        "hf://open-athena/: 4 buckets/repos",
        "hf: 1/4 buckets/repos, 6 requests, waited 0s",
        "hf: 2/4 buckets/repos, 8 requests, waited 0s",
        "hf: 3/4 buckets/repos, 10 requests, waited 0s",
        "",
    ]


def test_list_one_kind():
    c, _ = client()
    units = list(bulk_hf.list_hf(c, HfTarget("open-athena", "spaces")))
    assert [r for rows, _ in units for r in rows] == EXPECTED_ROWS[-2:]
    assert c.requests == 3


def test_list_repo_dedupes_oids_without_commit_dates():
    """Two paths share one LFS object: billed once, so the second lists at 0 B;
    `commit_dates=False` reads the un-expanded tree and dates by `lastModified`."""
    c, _ = client()
    rows, totals = bulk_hf.list_repo(c, "datasets", "open-athena", CC.split("/")[1], commit_dates=False)
    mod = "2026-10-13T14:31:23.000Z"
    assert rows == [
        (f"{CC}/artifacts/data/closure-near-boundary-cases-v7/validation.jsonl", 11224478, mod, "public", None),
        (f"{CC}/artifacts/data/closure-value-controls-cases-v5/validation.jsonl", 0, mod, "public", None),
        (f"{CC}/.gitattributes", 8904, mod, "git", None),
    ]
    assert totals == UnitTotals(CC, expected=11224478, listed=11224478, git_bytes=8904)


OLD = "ab" * 32


def _lfs_files_fixture() -> dict:
    tree = json.loads(FIXTURE.read_text())[f"/api/{BFCL.replace('datasets/', 'datasets/open-athena/')}/tree/b5c35eae7ca3c1f6c1ea9f8d3e52b2ee45fe67c4?recursive=true&expand=true"]["body"]
    [cur] = [e["lfs"]["oid"] for e in tree if "lfs" in e]
    return {f"/api/datasets/open-athena/{BFCL.split('/')[1]}/lfs-files": ok([
        {"fileOid": cur, "oid": "1" * 40, "filename": "artifacts.tar.gz", "pushedAt": "2026-10-11T11:49:28.000Z", "ref": "main", "size": 86937622},
        {"fileOid": OLD, "oid": "2" * 40, "filename": "artifacts.tar.gz", "pushedAt": "2026-10-11T09:00:00.000Z", "ref": "main", "size": 2140},
    ])}


def test_list_repo_lfs_files_attributes_history():
    c, _ = client(_lfs_files_fixture())
    rows, totals = bulk_hf.list_repo(c, "datasets", "open-athena", BFCL.split("/")[1], lfs_files=True)
    assert rows == EXPECTED_ROWS[3:5] + [
        (f"{BFCL}/@history/artifacts.tar.gz/{OLD}", 2140, "2026-10-11T09:00:00.000Z", "public", None),
    ]
    assert totals == UnitTotals(BFCL, expected=86939762, listed=86939762, git_bytes=2562, history=2140)


def test_list_repo_lfs_files_forbidden():
    path = f"/api/datasets/open-athena/{BFCL.split('/')[1]}/lfs-files"
    c, _ = client({path: {"status": 403, "headers": {}, "body": {"error": "You have read access but not the required permissions for this operation"}}})
    with pytest.raises(HfError) as e:
        bulk_hf.list_repo(c, "datasets", "open-athena", BFCL.split("/")[1], lfs_files=True)
    assert (e.value.status, str(e.value)) == (
        403, f"HTTP 403 for {path} (needs a token with write access to open-athena/{BFCL.split('/')[1]}): ",
    )


def test_listing_over_used_storage_warns_without_residual(capsys):
    info_path = f"/api/models/open-athena/marinfold-exp75{INFO}"
    info = json.loads(FIXTURE.read_text())[info_path]["body"]
    c, _ = client({info_path: ok({**info, "usedStorage": 100})})
    rows, totals = bulk_hf.list_repo(c, "models", "open-athena", "marinfold-exp75")
    assert rows == EXPECTED_ROWS[6:8]
    assert totals == UnitTotals(EXP75, expected=100, listed=25169920, git_bytes=3899)
    assert capsys.readouterr().err == "WARN: hf model open-athena/marinfold-exp75: listed 25,169,920 LFS B > usedStorage 100 B\n"


# ---------- rate limiting ----------

class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: "list[float]" = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_429_honors_retry_after():
    seq = [
        Response(429, {"retry-after": "7"}, b"rate limited"),
        Response(200, {}, b'[{"id": "ns/m"}]'),
    ]
    clock = Clock()
    c = HfClient(transport=lambda url, h: seq.pop(0), sleep=clock.sleep, clock=clock)
    assert c.get_json("/api/models") == [{"id": "ns/m"}]
    assert (clock.sleeps, c.requests) == ([7.0], 2)


def test_spent_window_pauses_until_reset():
    """`RateLimit: r ≤ reserve` holds the next request until the window resets (`t` + 1 s)."""
    seq = [
        Response(200, {"ratelimit": '"api";r=2;t=40'}, b"[1]"),
        Response(200, {"ratelimit": '"api";r=999;t=300'}, b"[2]"),
        Response(200, {"ratelimit": '"api";r=998;t=299'}, b"[3]"),
    ]
    clock = Clock()
    c = HfClient(concurrency=3, transport=lambda url, h: seq.pop(0), sleep=clock.sleep, clock=clock)
    assert [c.get_json("/a"), c.get_json("/b"), c.get_json("/c")] == [[1], [2], [3]]
    assert clock.sleeps == [41.0]


def test_5xx_backs_off_then_raises():
    clock = Clock()
    c = HfClient(max_retries=3, transport=lambda url, h: Response(502, {}, b"bad gateway"), sleep=clock.sleep, clock=clock)
    with pytest.raises(HfError) as e:
        c.get_json("/x")
    assert (str(e.value), clock.sleeps, c.requests) == (f"HTTP 502 for {E}/x: bad gateway", [1.0, 2.0, 4.0], 4)


def test_connection_errors_back_off_and_retry(capsys):
    """A reset / dropped / timed-out connection (raised by the transport, no status) is retried like a 5xx: hf's first
    Nextflow scan (2026-10-17T0247) died on one `Connection reset by peer` seven minutes into the listing."""
    seq: list = [
        urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        TimeoutError("The read operation timed out"),
        Response(200, {}, b'[{"id": "ns/m"}]'),
    ]

    def transport(url, h):
        r = seq.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    clock = Clock()
    c = HfClient(transport=transport, sleep=clock.sleep, clock=clock)
    assert c.get_json("/api/models") == [{"id": "ns/m"}]
    assert (clock.sleeps, c.requests) == ([1.0, 2.0, 4.0], 4)
    assert capsys.readouterr().err.splitlines() == [
        f"hf: <urlopen error [Errno 104] Connection reset by peer> on {E}/api/models; retrying in 1s (1/8)",
        f"hf: Remote end closed connection without response on {E}/api/models; retrying in 2s (2/8)",
        f"hf: The read operation timed out on {E}/api/models; retrying in 4s (3/8)",
    ]


def test_connection_errors_raise_after_the_retries():
    def transport(url, h):
        raise urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))

    clock = Clock()
    c = HfClient(max_retries=2, transport=transport, sleep=clock.sleep, clock=clock)
    with pytest.raises(urllib.error.URLError):
        c.get_json("/x")
    assert (clock.sleeps, c.requests) == ([1.0, 2.0], 3)


# ---------- shards + CLI ----------

def test_list_hf_to_parquet(tmp_path: Path):
    c, _ = client()
    out = tmp_path / "listing"
    assert bulk_hf.list_hf_to_parquet("hf://open-athena", str(out), threads=2, client=c) == 10
    assert sorted(p.name for p in out.iterdir()) == ["_SUCCESS.json", "shard-0000.parquet"]
    df = pd.read_parquet(out / "shard-0000.parquet")
    assert df.columns.tolist() == ["bucket", "name", "size_bytes", "created", "storage_class_id", "generation"]
    assert [
        (b, n, s, c_.isoformat(), k, g) for b, n, s, c_, k, g in df.astype(object).itertuples(index=False)
    ] == [
        ("open-athena", n, s, pd.Timestamp(t).isoformat(), {"public": 1, "git": 6}[k], pd.NA)
        for n, s, t, k, _ in EXPECTED_ROWS
    ]
    marker = json.loads((out / "_SUCCESS.json").read_text())
    assert {k: v for k, v in marker.items() if k not in ("started", "finished")} == {
        "bucket": "open-athena", "prefix": None, "objects": 10,
        "hf": {
            "expected": 1110 + 86939762 + 25169920, "listed": 1110 + 86937622 + 25169920,
            "git_bytes": 2562 + 3899 + 2060, "residual": 2140, "history": 0,
            "over": [], "requests": 12, "waited_s": 0.0,
        },
    }


@pytest.mark.parametrize("args", [["-a"], ["-P", "2"], ["-p", "models/"]])
def test_cli_hf_rejects_bucket_options(args, tmp_path: Path):
    from disk_tree.cli.base import cli

    r = CliRunner().invoke(cli, ["bulk-list", *args, "-o", str(tmp_path), "hf://open-athena"])
    assert (r.exit_code, str(r.exception)) == (1, "hf:// takes only -w, -x, -L and -D (narrow with the URI: hf://<ns>/<kind>/<name>)")


def test_cli_hf_flags_rejected_for_buckets(tmp_path: Path):
    from disk_tree.cli.base import cli

    r = CliRunner().invoke(cli, ["bulk-list", "-L", "-o", str(tmp_path), "gcs://b"])
    assert (r.exit_code, str(r.exception)) == (1, "-L/--lfs-files and -D/--no-commit-dates apply to hf:// only")
