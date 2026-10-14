"""Generic code carries no deployment's values (specs/deployment-divergence.md §4): the layer-2 template, an index
generation's key, the digest template and the static-names generation are each the deployment's to name, and a missing
one is an error naming the flag or variable. The variant set is read off the generation's files, not configured."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud import cli as C
from dt_cloud import digest as D
from dt_cloud import index_footer as F
from dt_cloud.cli import main
from dt_cloud.deploy import layer2_prefix
from dt_cloud.static_profile import ENV, from_mapping, load_profile, to_mapping
from dt_cloud.static_profile_examples import CW, EXAMPLES, GCS

NO_L2 = "no layer-2 dir: pass {flag} or export LAYER2_PREFIX (the deployment's layer-2 dir template, `{{scan}}` = the scan id)"


def test_layer2_prefix_is_required():
    with pytest.raises(SystemExit) as e:
        layer2_prefix()
    assert str(e.value) == NO_L2.format(flag="-l")
    with pytest.raises(SystemExit) as e:
        layer2_prefix("x/index/")
    assert str(e.value) == "layer-2 dir 'x/index/' has no `{scan}`: it is a template, one dir per scan"
    assert layer2_prefix("a/{scan}/") == "a/{scan}/"


def test_layer2_prefix_from_env_and_flag_over_it(monkeypatch):
    monkeypatch.setenv("LAYER2_PREFIX", "env-l2/{scan}/")
    assert (layer2_prefix(), layer2_prefix("flag/{scan}/")) == ("env-l2/{scan}/", "flag/{scan}/")


@pytest.mark.parametrize("args, flag", [
    (["publish-r2", "2026-10-01"], "-l/--layer2"),
    (["over-time-groups", "-g", "G", "-n", "-o", "out"], "-l/--layer2-prefix"),
])
def test_layer2_commands_refuse_without_a_template(monkeypatch, args, flag):
    """Neither command falls back to some deployment's layout (`listing/{scan}/index/`, `cw-l2/{scan}/`), and both
    refuse before reading D1."""
    def no_d1(*a, **kw):
        raise AssertionError("read D1")
    monkeypatch.setattr(F, "pointers", no_d1)
    monkeypatch.setattr(F, "synced_variants", no_d1)
    r = CliRunner().invoke(main, args)
    assert (r.exit_code, r.output) == (1, NO_L2.format(flag=flag) + "\n")


def test_publish_r2_uses_the_given_template(monkeypatch):
    from dt_cloud import publish as P

    seen: list[str] = []
    monkeypatch.setattr(P, "publish", lambda scan, **kw: seen.append(kw["layer2"]) or P.Report())
    monkeypatch.setenv("LAYER2_PREFIX", "env-l2/{scan}/")
    for args in (["-a"], ["-a", "-l", "flag/{scan}/"]):
        r = CliRunner().invoke(main, ["publish-r2", *args, "2026-10-01"])
        assert r.exit_code == 0, r.output
    assert seen == ["env-l2/{scan}/", "flag/{scan}/"]


def test_index_sync_needs_its_key():
    """The generation's bucket-relative dir is the deployment's layout: no `listing/<date>/index/<gen>` default."""
    r = CliRunner().invoke(main, ["index-sync", "-d", "x", "-g", "G1", "2026-09-30"])
    assert (r.exit_code, r.output.rstrip().split("\n")[-1]) == (2, "Error: Missing option '-k' / '--key'.")


@pytest.mark.parametrize("args, msg", [
    (["-g", "G1"], "-g needs -k: the bucket-relative dir is the deployment's layout (no default)"),
    (["-k", "l2/{date}/index/{gen}"], "-k names `{gen}`: pass -g"),
])
def test_index_blob_needs_a_key_for_a_gen(args, msg):
    r = CliRunner().invoke(main, ["index-blob", *args, "2026-09-30"])
    assert (r.exit_code, r.output.rstrip().split("\n")[-1]) == (2, f"Error: {msg}")


def test_index_blob_key_template(monkeypatch):
    """`-k` is a template over the scan id and `-g`, read under the data bucket."""
    asked: list[str] = []
    monkeypatch.setattr(F, "exists", lambda p: asked.append(p) and False)
    monkeypatch.setattr(C, "err", lambda *a: None)
    r = CliRunner().invoke(main, ["index-blob", "-v", "path", "-g", "G1", "-k", "l2/{date}/index/{gen}", "2026-09-30", "2026-10-01"])
    assert r.exit_code == 0, r.output
    assert asked == ["gs://my-data/l2/2026-09-30/index/G1/path-index.parquet", "gs://my-data/l2/2026-10-01/index/G1/path-index.parquet"]


def test_variants_are_not_configured():
    """`$INDEX_VARIANTS` (once defaulting to gcs's `path,user`) is not read: every sort is in the registry whatever it
    says, and a generation's absent files are skipped (`test_index_sync_cli`). A fresh interpreter, so the import-time
    registry sees the env."""
    code = "from dt_cloud.index_footer import INDEX_VARIANTS, SORT_VARIANTS; print(list(SORT_VARIANTS), list(INDEX_VARIANTS)[:4])"
    out = [subprocess.run([sys.executable, "-c", code], env={**os.environ, **env}, capture_output=True, text=True, check=True).stdout
           for env in ({"INDEX_VARIANTS": "path"}, {"INDEX_VARIANTS": "path,user"})]
    want = "['path', 'bysize', 'user', 'bysize-user'] ['path', 'bysize', 'user', 'bysize-user']\n"
    assert out == [want, want]


@pytest.mark.parametrize("base, want", [
    ("my-data", "gs://my-data/d/path-index.groups.parquet"),
    ("oa-x", "gs://oa-x/d/path-index.groups.parquet"),
    ("r2://bk", "r2://bk/d/path-index.groups.parquet"),
    ("/gcs/b", "/gcs/b/d/path-index.groups.parquet"),
    ("./rel", "./rel/d/path-index.groups.parquet"),
])
def test_cold_path_bucket_names_are_gcs_by_shape_not_by_prefix(base, want):
    """A bare bucket name is GCS whatever it is called (no `oa-` heuristic); URLs and paths are kept."""
    assert F.cold_path(base, "d", "path") == want


def test_digest_config_needs_a_template():
    with pytest.raises(ValueError) as e:
        D.config_from_dict({"reply_hour": 3})
    assert str(e.value) == "digest config: no template (one of ('gcs', 'cw')): a deployment names its own"
    assert D.config_from_dict({"template": "cw"}) == D.PRESETS["cw"]


def test_digest_plot_needs_a_template(tmp_path: Path):
    from dt_cloud.digest_plot import main as plot

    rows = tmp_path / "rows.json"
    rows.write_text("[]")
    r = CliRunner().invoke(plot, ["-d", str(rows)])
    assert (r.exit_code, r.output.rstrip().split("\n")[-3:]) == (2, ["Error: Missing option '-T' / '--template'. Choose from:", "\tgcs,", "\tcw"])


def test_examples_carry_no_live_generation():
    """The examples are illustrative: a compaction moves the live gen on the deployment's branch, never here."""
    assert [p.gen for p in EXAMPLES.values()] == [None, None]
    with pytest.raises(SystemExit) as e:
        load_profile({"STATIC_NAMES_PROFILE": "gcs"}).need("gen")
    assert str(e.value) == "static names: no gen in the deployment profile: set STATIC_NAMES_GEN (or STATIC_NAMES_PROFILE)"


@pytest.mark.parametrize("example", [GCS, CW])
def test_profile_file_round_trips(tmp_path: Path, example):
    """`to_mapping` is what a branch checks in (`dt-cloud static-names profile`); loading it back is the same profile."""
    want = from_mapping({**to_mapping(example), "gen": "2026-10-08c"})
    f = tmp_path / "static-names.json"
    f.write_text(json.dumps(to_mapping(want)))
    assert load_profile({"STATIC_NAMES_PROFILE": str(f)}) == want


def test_profile_cmd_prints_the_file_and_its_fields(tmp_path: Path, monkeypatch):
    from dt_cloud.static_names import cli as sn
    from dt_cloud.static_profile import profile

    f = tmp_path / "static-names.json"
    f.write_text(json.dumps({**to_mapping(CW), "gen": "2026-10-10cw"}))
    monkeypatch.setenv("STATIC_NAMES_PROFILE", str(f))
    monkeypatch.setenv("R2_ENDPOINT", "https://acct.r2.example")  # ambient (a deployment's `.envrc`): never printed into a profile
    profile.cache_clear()
    try:
        out = [CliRunner().invoke(sn, ["profile", *a]) for a in (["-f", "gen"], ["-f", "layouts"], ["-f", "compact_level"], [])]
        assert [(r.exit_code, r.output) for r in out[:3]] == [
            (0, "2026-10-10cw\n"), (0, '["cw-l2/{id}/index/{gen}/path-index.parquet"]\n'), (0, "5\n"),
        ]
        assert json.loads(out[3].output) == {k: v for k, v in {**to_mapping(CW), "gen": "2026-10-10cw"}.items() if k != "r2_endpoint"}
        f.write_text(json.dumps(to_mapping(CW)))
        profile.cache_clear()
        r = CliRunner().invoke(sn, ["profile", "-f", "gen"])
        assert (r.exit_code, r.output) == (1, "static names: no gen in the deployment profile: set STATIC_NAMES_GEN (or STATIC_NAMES_PROFILE)\n")
        r = CliRunner().invoke(sn, ["profile", "-f", "nope"])
        assert (r.exit_code, r.output) == (1, f"static names profile: no field 'nope' (one of {', '.join(ENV)})\n")
    finally:
        profile.cache_clear()
