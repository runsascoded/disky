"""`dt-cloud image build|promote` (`image_build`): the deployment's `job/build.json`, the candidate tag, the Cloud Build
config, and the commands a build and a promote run (through a recording runner: nothing reaches gcloud)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click import ClickException

from dt_cloud import image_build as ib
from dt_cloud.image_build import BuildConfig, CloudBuild, Repoint

SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 11, 12, 9, 8, 7, tzinfo=timezone.utc)
PRIMARY = "us-central1-docker.pkg.dev/p/scan-repo/scan"
MIRROR = "us-east1-docker.pkg.dev/p/mirror-repo/scan"
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64

LIVE = {
    "project": "p",
    "repository": PRIMARY,
    "mirrors": [MIRROR],
    "mode": "live",
    "live_tags": ["latest"],
    "cache": True,
    "build_args": {"SITE": "build", "ENTRYPOINT": "dt-cloud scan-run supervise -- bash job/run.sh"},
    "context": ["job", "src"],
    "cloudbuild": {"machine_type": "E2_HIGHCPU_8", "timeout": "1800s"},
    "repoint": {"region": "us-central1", "jobs": ["submit-job"], "services": ["relay"]},
}


def test_parse_config():
    assert ib.parse_config(LIVE) == BuildConfig(
        project="p",
        repository=PRIMARY,
        mirrors=(MIRROR,),
        mode="live",
        live_tags=("latest",),
        build_args={"SITE": "build", "ENTRYPOINT": "dt-cloud scan-run supervise -- bash job/run.sh"},
        context=("job", "src"),
        cache=True,
        cloudbuild=CloudBuild(machine_type="E2_HIGHCPU_8", timeout="1800s"),
        repoint=Repoint(region="us-central1", jobs=("submit-job",), services=("relay",)),
    )
    assert ib.parse_config({"project": "p", "repository": PRIMARY}) == BuildConfig(project="p", repository=PRIMARY)


@pytest.mark.parametrize("over, msg", [
    ({"project": None}, "project: expected a non-empty string"),
    ({"extra": 1}, "build config: unknown keys extra"),
    ({"repository": PRIMARY + ":latest"}, f"repository: '{PRIMARY}:latest' carries a tag or digest; name the repository only"),
    ({"mode": "prod"}, "mode: expected one of candidate, live, not 'prod'"),
    ({"live_tags": ["Latest"]}, "live_tags: 'Latest' is not a lowercase Docker tag"),
    ({"build_args": {"site": "x"}}, "build_args: 'site' is not an upper-case ARG name"),
    ({"build_args": {"DISKY_GIT_SHA": "x"}}, "build_args: DISKY_GIT_SHA is the build's own stamp"),
    ({"build_args": {"SITE": 1}}, "build_args: expected an object of strings"),
    ({"context": []}, "context: expected at least one entry"),
    ({"cache": "yes"}, "cache: expected true or false"),
    ({"live_tags": [], "repoint": None}, "cache: needs live_tags (the cache is the first live tag's image)"),
    ({"cloudbuild": {"machine": "x"}}, "cloudbuild: unknown keys machine"),
    ({"repoint": {"jobs": ["a"]}}, "repoint: region is required"),
    ({"cache": False, "live_tags": []}, "repoint: needs live_tags (consumers are re-pointed at the first live tag)"),
])
def test_parse_config_errors(over, msg):
    with pytest.raises(ValueError) as e:
        ib.parse_config({**LIVE, **over})
    assert str(e.value) == msg


def test_missing_required():
    with pytest.raises(ValueError) as e:
        ib.parse_config({"project": "p"})
    assert str(e.value) == "build config: repository is required"


def test_candidate_tag():
    assert ib.candidate_tag(SHA, False, NOW) == "0123456789ab"
    assert ib.candidate_tag(SHA, True, NOW) == "0123456789ab-dirty-20261112t090807z"
    with pytest.raises(ValueError):
        ib.candidate_tag(SHA[:12], False, NOW)


def test_check_tag():
    cfg = ib.parse_config(LIVE)
    assert ib.check_tag(cfg, "try-x.1") == "try-x.1"
    for tag, msg in (("latest", "-t latest: a live tag; build a candidate, then `promote` it"),
                     ("Try", "-t 'Try': expected a lowercase Docker tag of at most 128 characters")):
        with pytest.raises(ClickException) as e:
            ib.check_tag(cfg, tag)
        assert e.value.message == msg


def test_parse_build_args():
    assert ib.parse_build_args(["ENTRYPOINT=bash job/x.sh --y", "SITE="]) == {"ENTRYPOINT": "bash job/x.sh --y", "SITE": ""}
    with pytest.raises(ClickException) as e:
        ib.parse_build_args(["DISKY_GIT_DIRTY=0"])
    assert e.value.message == "-a 'DISKY_GIT_DIRTY=0': expected KEY=VALUE, KEY an upper-case ARG name other than DISKY_GIT_SHA, DISKY_GIT_DIRTY"


def test_cloudbuild_config_live():
    cfg = ib.parse_config(LIVE)
    images = [f"{PRIMARY}:0123456789ab", f"{MIRROR}:0123456789ab"]
    assert ib.cloudbuild_config(cfg, images, SHA, False, cfg.build_args) == {
        "steps": [{"name": "gcr.io/cloud-builders/docker", "entrypoint": "bash", "args": ["-c", "\n".join([
            "set -euo pipefail",
            f"docker pull {PRIMARY}:latest || true",
            "DOCKER_BUILDKIT=1 docker build -f job/Dockerfile --build-arg SITE=build"
            " --build-arg 'ENTRYPOINT=dt-cloud scan-run supervise -- bash job/run.sh'"
            f" --build-arg DISKY_GIT_SHA={SHA} --build-arg DISKY_GIT_DIRTY=0"
            f" --build-arg BUILDKIT_INLINE_CACHE=1 --cache-from {PRIMARY}:latest"
            f" -t {PRIMARY}:0123456789ab -t {MIRROR}:0123456789ab .",
        ])]}],
        "images": images,
        "options": {"machineType": "E2_HIGHCPU_8"},
        "timeout": "1800s",
    }


def test_cloudbuild_config_minimal():
    cfg = ib.parse_config({"project": "p", "repository": PRIMARY, "cloudbuild": {"logging": "CLOUD_LOGGING_ONLY", "tags": ["a", "b"]}})
    assert ib.cloudbuild_config(cfg, [f"{PRIMARY}:t"], SHA, True, {}) == {
        "steps": [{"name": "gcr.io/cloud-builders/docker", "entrypoint": "bash", "args": ["-c", "\n".join([
            "set -euo pipefail",
            f"DOCKER_BUILDKIT=1 docker build -f job/Dockerfile --build-arg DISKY_GIT_SHA={SHA} --build-arg DISKY_GIT_DIRTY=1 -t {PRIMARY}:t .",
        ])]}],
        "images": [f"{PRIMARY}:t"],
        "options": {"logging": "CLOUD_LOGGING_ONLY"},
        "tags": ["a", "b"],
    }
    with pytest.raises(ValueError) as e:
        ib.cloudbuild_config(cfg, [f"{PRIMARY}:t"], SHA, False, {"X": "$HOME"})
    assert str(e.value) == "a build value carries `$`, which Cloud Build would read as a substitution"
    with pytest.raises(ValueError):
        ib.cloudbuild_config(cfg, [f"{PRIMARY}:t"], "", False, {})


def test_submit_command():
    cfg = ib.parse_config({"project": "p", "repository": PRIMARY, "cloudbuild": {
        "region": "us-west1", "service_account": "builder@p.iam.gserviceaccount.com", "staging_dir": "gs://scratch/builds/source"}})
    assert ib.submit_command(cfg, Path("/r/tmp/ctx")) == [
        "gcloud", "builds", "submit", "/r/tmp/ctx", "--project", "p", "--config", "/r/tmp/ctx/cloudbuild.json",
        "--region", "us-west1", "--service-account", "projects/p/serviceAccounts/builder@p.iam.gserviceaccount.com",
        "--gcs-source-staging-dir", "gs://scratch/builds/source",
    ]


class Recorder(ib.Runner):
    """Records every command; `describe` answers from `digests` (image → digest), `run … describe` from `existing`."""

    def __init__(self, digests: dict[str, str], existing: set[str] = frozenset(), on_submit=None):
        super().__init__(dry_run=False)
        self.digests, self.existing, self.on_submit = dict(digests), existing, on_submit
        self.calls: list[list[str]] = []

    def run(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["gcloud", "builds", "submit"] and self.on_submit:
            self.on_submit(self, Path(cmd[3]))

    def capture(self, cmd):
        self.calls.append(cmd)
        if cmd[:4] == ["gcloud", "artifacts", "docker", "images"]:
            d = self.digests.get(cmd[5])
            return (0, d) if d else (1, "")
        if cmd[:2] == ["gcloud", "run"]:
            return (0, cmd[4]) if cmd[4] in self.existing else (1, "")
        raise AssertionError(cmd)


def promote_calls(tag: str) -> list[list[str]]:
    loc = ["--project", "p", "--region", "us-central1"]
    return [
        ib.describe_command(f"{PRIMARY}:{tag}"),
        ib.describe_command(f"{MIRROR}:{tag}"),
        ["gcloud", "artifacts", "docker", "tags", "add", f"{PRIMARY}@{D1}", f"{PRIMARY}:latest", "--quiet"],
        ["gcloud", "artifacts", "docker", "tags", "add", f"{MIRROR}@{D2}", f"{MIRROR}:latest", "--quiet"],
        ["gcloud", "run", "jobs", "describe", "submit-job", *loc, "--format=value(name)"],
        ["gcloud", "run", "jobs", "update", "submit-job", *loc, "--image", f"{PRIMARY}:latest", "--quiet"],
        ["gcloud", "run", "services", "describe", "relay", *loc, "--format=value(metadata.name)"],
    ]


def test_promote():
    cfg = ib.parse_config(LIVE)
    r = Recorder({f"{PRIMARY}:c1": D1, f"{MIRROR}:c1": D2}, existing={"submit-job"})
    assert ib.promote(cfg, r, "c1", expect=D1) == {PRIMARY: D1, MIRROR: D2}
    assert r.calls == promote_calls("c1")


def test_promote_refusals():
    cfg = ib.parse_config(LIVE)
    for runner, expect, msg in (
        (Recorder({f"{PRIMARY}:c1": D1, f"{MIRROR}:c1": D2}), D2, f"{PRIMARY}:c1 is {D1}, not the expected {D2}"),
        (Recorder({f"{PRIMARY}:c1": D1}), None, f"{MIRROR}:c1: no such image (build the candidate first)"),
    ):
        with pytest.raises(ClickException) as e:
            ib.promote(cfg, runner, "c1", expect)
        assert e.value.message == msg
        assert [c for c in runner.calls if c[3:5] == ["tags", "add"] or c[2:4] == ["jobs", "update"]] == []
    with pytest.raises(ClickException) as e:
        ib.promote(ib.parse_config({"project": "p", "repository": PRIMARY}), Recorder({}), "c1")
    assert e.value.message == "nothing to promote: the config has no live_tags (its consumers pin a digest)"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "job").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "specs").mkdir()
    (root / "job" / "Dockerfile").write_text("FROM scratch\n")
    (root / "src" / "a.py").write_text("a = 1\n")
    (root / "specs" / "s.md").write_text("not in the context\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    return root


def staged_files(ctx: Path) -> list[str]:
    return sorted(str(p.relative_to(ctx)) for p in ctx.rglob("*") if p.is_file())


def test_build_live(checkout):
    cfg = ib.parse_config(LIVE)
    sha = git(checkout, "rev-parse", "HEAD")
    tag = sha[:12]
    seen = {}

    def on_submit(r, ctx):
        seen["files"] = staged_files(ctx)
        seen["config"] = json.loads((ctx / "cloudbuild.json").read_text())
        r.digests.update({f"{PRIMARY}:{tag}": D1, f"{MIRROR}:{tag}": D2})

    r = Recorder({}, existing={"submit-job"}, on_submit=on_submit)
    assert ib.build(cfg, checkout, r, mode="live", tag=None, allow_dirty=False, build_args={}, now=lambda: NOW) == {PRIMARY: D1, MIRROR: D2}
    ctx = checkout / "tmp" / "image-build-20261112T090807000000Z"
    assert seen["files"] == [".gcloudignore", "cloudbuild.json", "job/Dockerfile", "src/a.py"]
    assert seen["config"] == ib.cloudbuild_config(cfg, [f"{PRIMARY}:{tag}", f"{MIRROR}:{tag}"], sha, False, cfg.build_args)
    assert r.calls == [
        ib.describe_command(f"{PRIMARY}:{tag}"),
        ib.submit_command(cfg, ctx),
        ib.describe_command(f"{PRIMARY}:{tag}"),
        ib.describe_command(f"{MIRROR}:{tag}"),
        *promote_calls(tag),
    ]
    assert not ctx.exists()


def test_build_candidate_exists(checkout, capsys):
    """A candidate tag already pushed is not rebuilt (it's immutable): its digests are printed, nothing moves."""
    cfg = ib.parse_config(LIVE)
    tag = git(checkout, "rev-parse", "HEAD")[:12]
    r = Recorder({f"{PRIMARY}:{tag}": D1, f"{MIRROR}:{tag}": D2})
    assert ib.build(cfg, checkout, r, mode="candidate", tag=None, allow_dirty=False, build_args={}, now=lambda: NOW) == {PRIMARY: D1, MIRROR: D2}
    assert r.calls == [ib.describe_command(f"{PRIMARY}:{tag}")] * 2 + [ib.describe_command(f"{MIRROR}:{tag}")]
    assert capsys.readouterr().out.splitlines() == [f"{PRIMARY}@{D1}", f"{MIRROR}@{D2}"]


def test_build_dirty(checkout):
    (checkout / "src" / "a.py").write_text("a = 2\n")
    cfg = ib.parse_config(LIVE)
    with pytest.raises(ClickException) as e:
        ib.build(cfg, checkout, Recorder({}), mode="candidate", tag=None, allow_dirty=False, build_args={}, now=lambda: NOW)
    assert e.value.message == "refusing a dirty tree: commit (or -D to build it stamped dirty)"
    with pytest.raises(ClickException) as e:
        ib.build(ib.parse_config({**LIVE, "allow_dirty": False}), checkout, Recorder({}), mode="candidate", tag=None, allow_dirty=True,
                 build_args={}, now=lambda: NOW)
    assert e.value.message == "refusing a dirty tree: this deployment builds only committed source"

    tag = git(checkout, "rev-parse", "HEAD")[:12] + "-dirty-20261112t090807z"
    seen = {}

    def on_submit(r, ctx):
        seen["a"] = (ctx / "src" / "a.py").read_text()
        r.digests.update({f"{PRIMARY}:{tag}": D1, f"{MIRROR}:{tag}": D2})

    r = Recorder({}, on_submit=on_submit)
    ib.build(cfg, checkout, r, mode="candidate", tag=None, allow_dirty=True, build_args={}, now=lambda: NOW)
    assert seen == {"a": "a = 2\n"}


def test_build_args_need_a_tag(checkout):
    with pytest.raises(ClickException) as e:
        ib.build(ib.parse_config(LIVE), checkout, Recorder({}), mode="candidate", tag=None, allow_dirty=False,
                 build_args={"ENTRYPOINT": "bash job/other.sh"}, now=lambda: NOW)
    assert e.value.message == "-a changes the image without changing the commit: name its candidate with -t"


def test_resolve_refuses_another_checkout(checkout, tmp_path, monkeypatch):
    (checkout / "job" / "build.json").write_text(json.dumps({"project": "p", "repository": PRIMARY}))
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    monkeypatch.chdir(other)
    with pytest.raises(ClickException) as e:
        ib.resolve(str(checkout / "job" / "build.json"), other_checkout=False)
    root, other_root = Path(git(checkout, "rev-parse", "--show-toplevel")), Path(git(other, "rev-parse", "--show-toplevel"))
    assert e.value.message == (f"cwd is in checkout {other_root}, but this config images {root}: run {other_root}/job/build.sh "
                               f"to image your checkout (-O to build {root} anyway)")
    assert ib.resolve(str(checkout / "job" / "build.json"), other_checkout=True) == (root, BuildConfig(project="p", repository=PRIMARY))
    monkeypatch.chdir(checkout)
    assert ib.resolve(None, other_checkout=False) == (root, BuildConfig(project="p", repository=PRIMARY))
