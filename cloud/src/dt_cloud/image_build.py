"""The scan job image's build, from a deployment's config (`job/build.json`): `dt-cloud image build` and
`dt-cloud image promote`, which `job/build.sh` runs (specs/job-consolidation.md §5).

A build images one checkout (the one the config lives in, never the caller's other worktree) at one commit, stamped with
its sha and dirty state (`DISKY_GIT_SHA` / `DISKY_GIT_DIRTY` build args → OCI labels + env, what `dt-cloud image-sha`
reads), from a staged context: the commit's files under the config's `context` pathspecs (`git archive`; a dirty build,
allowed only with `-D`, copies the worktree's tracked files instead). Cloud Build builds `job/Dockerfile` (the config's
`dockerfile`) with the config's `build_args` and pushes it to the `repository` and every `mirrors` repository.

Two modes (the config's `mode`, `-m` overrides):
- `candidate`: push an immutable tag (the commit's first 12 hex chars; a dirty build's adds `-dirty-<UTC time>`; `-t`
  names another) and print each repository's `<repository>@<digest>`. No live tag moves, nothing is re-pointed. A tag
  already in the primary repository is not rebuilt: its digest is printed (a candidate is immutable).
- `live`: the candidate, then `promote`.

`promote TAG` moves each repository's `live_tags` to the digest TAG names there (`gcloud artifacts docker tags add`), then
re-points the config's `repoint` Cloud Run jobs and services at the primary repository's first live tag (Cloud Run
resolves a tag to a digest when a job or service is updated, not when it runs). `-x` refuses a primary digest other
than the one a reviewer approved.

Nothing here names a deployment: every project, repository, region and account is the config's.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from click import ClickException, Choice, Path as ClickPath, argument, group, option

MODES = ("candidate", "live")
#: A Docker tag (lowercase only: candidate tags are derived from shas and times).
TAG = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,127}")
#: A build arg's name: the Dockerfile `ARG`s are upper-case identifiers.
ARG_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
#: Set by the build itself (the stamp); a config may not set them.
STAMP_ARGS = ("DISKY_GIT_SHA", "DISKY_GIT_DIRTY")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
DOCKER = "gcr.io/cloud-builders/docker"


def err(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


@dataclass(frozen=True)
class CloudBuild:
    """`gcloud builds submit` / Cloud Build options; each unset one is Cloud Build's default."""
    region: str | None = None
    #: The build's service account (an email); `staging_dir` and `logging` usually come with it.
    service_account: str | None = None
    #: `--gcs-source-staging-dir`: where the context is uploaded (`gs://…`).
    staging_dir: str | None = None
    machine_type: str | None = None
    timeout: str | None = None
    #: `options.logging`, e.g. `CLOUD_LOGGING_ONLY` (required by Cloud Build with a user-managed service account).
    logging: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Repoint:
    """Cloud Run consumers of the live tag, re-pointed after a promote; one missing is skipped."""
    region: str
    jobs: tuple[str, ...] = ()
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuildConfig:
    project: str
    #: The primary image repository, no tag: `<region>-docker.pkg.dev/<project>/<repo>/<image>`.
    repository: str
    #: More repositories the same image goes to (each gets the candidate tag, and the live tags on promote).
    mirrors: tuple[str, ...] = ()
    mode: str = "candidate"
    #: The mutable tags `promote` moves (e.g. `latest`); empty: nothing to promote, consumers pin digests.
    live_tags: tuple[str, ...] = ()
    #: The Dockerfile's `ARG`s (`job/Dockerfile`: `SITE`, `APT_PACKAGES`, `UV_SYNC_ARGS`, `ENTRYPOINT`).
    build_args: Mapping[str, str] = field(default_factory=dict)
    #: Git pathspecs staged as the build context (`:(exclude)…` magic allowed); default the whole tree.
    context: tuple[str, ...] = (".",)
    dockerfile: str = "job/Dockerfile"
    #: Reuse layers from the primary repository's first live tag (`--cache-from`, with an inline cache).
    cache: bool = False
    #: Whether `-D` may build a tree with uncommitted changes to tracked files (stamped dirty).
    allow_dirty: bool = True
    cloudbuild: CloudBuild = field(default_factory=CloudBuild)
    repoint: Repoint | None = None

    @property
    def repositories(self) -> tuple[str, ...]:
        return (self.repository, *self.mirrors)


def _strs(name: str, v, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(s, str) and s for s in v):
        raise ValueError(f"{name}: expected a list of non-empty strings")
    if nonempty and not v:
        raise ValueError(f"{name}: expected at least one entry")
    return tuple(v)


def _str(name: str, v) -> str:
    if not isinstance(v, str) or not v:
        raise ValueError(f"{name}: expected a non-empty string")
    return v


def _known(name: str, d: Mapping, keys: Sequence[str]) -> None:
    if not isinstance(d, Mapping):
        raise ValueError(f"{name}: expected an object")
    if bad := sorted(set(d) - set(keys)):
        raise ValueError(f"{name}: unknown keys {', '.join(bad)}")


def parse_config(d: Mapping) -> BuildConfig:
    """A `BuildConfig` from its JSON; unknown keys and wrong types are errors naming the key."""
    _known("build config", d, ("project", "repository", "mirrors", "mode", "live_tags", "build_args", "context",
                               "dockerfile", "cache", "allow_dirty", "cloudbuild", "repoint"))
    for k in ("project", "repository"):
        if k not in d:
            raise ValueError(f"build config: {k} is required")
    kw: dict = {"project": _str("project", d["project"]), "repository": _str("repository", d["repository"])}
    if ":" in kw["repository"].rsplit("/", 1)[-1] or "@" in kw["repository"]:
        raise ValueError(f"repository: {kw['repository']!r} carries a tag or digest; name the repository only")
    if "mirrors" in d:
        kw["mirrors"] = _strs("mirrors", d["mirrors"])
    if "mode" in d:
        if d["mode"] not in MODES:
            raise ValueError(f"mode: expected one of {', '.join(MODES)}, not {d['mode']!r}")
        kw["mode"] = d["mode"]
    if "live_tags" in d:
        kw["live_tags"] = _strs("live_tags", d["live_tags"])
        for t in kw["live_tags"]:
            if not TAG.fullmatch(t):
                raise ValueError(f"live_tags: {t!r} is not a lowercase Docker tag")
    if "build_args" in d:
        args = d["build_args"]
        if not isinstance(args, Mapping) or not all(isinstance(v, str) for v in args.values()):
            raise ValueError("build_args: expected an object of strings")
        for k in args:
            if not ARG_NAME.fullmatch(k):
                raise ValueError(f"build_args: {k!r} is not an upper-case ARG name")
            if k in STAMP_ARGS:
                raise ValueError(f"build_args: {k} is the build's own stamp")
        kw["build_args"] = dict(args)
    if "context" in d:
        kw["context"] = _strs("context", d["context"], nonempty=True)
    if "dockerfile" in d:
        kw["dockerfile"] = _str("dockerfile", d["dockerfile"])
    for k in ("cache", "allow_dirty"):
        if k in d:
            if not isinstance(d[k], bool):
                raise ValueError(f"{k}: expected true or false")
            kw[k] = d[k]
    if kw.get("cache") and not kw.get("live_tags"):
        raise ValueError("cache: needs live_tags (the cache is the first live tag's image)")
    if "cloudbuild" in d:
        cb = d["cloudbuild"]
        _known("cloudbuild", cb, ("region", "service_account", "staging_dir", "machine_type", "timeout", "logging", "tags"))
        kw["cloudbuild"] = CloudBuild(
            **{k: _str(f"cloudbuild.{k}", v) for k, v in cb.items() if k != "tags"},
            **({"tags": _strs("cloudbuild.tags", cb["tags"])} if "tags" in cb else {}),
        )
    if (rp := d.get("repoint")) is not None:
        _known("repoint", rp, ("region", "jobs", "services"))
        if "region" not in rp:
            raise ValueError("repoint: region is required")
        kw["repoint"] = Repoint(region=_str("repoint.region", rp["region"]),
                                jobs=_strs("repoint.jobs", rp.get("jobs", [])),
                                services=_strs("repoint.services", rp.get("services", [])))
        if not kw.get("live_tags"):
            raise ValueError("repoint: needs live_tags (consumers are re-pointed at the first live tag)")
    return BuildConfig(**kw)


def load_config(path: Path) -> BuildConfig:
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ClickException(f"{path}: no build config (the deployment's `job/build.json`; specs/job-consolidation.md §5)") from None
    try:
        return parse_config(raw)
    except ValueError as e:
        raise ClickException(f"{path}: {e}") from None


def candidate_tag(sha: str, dirty: bool, now: datetime) -> str:
    """The immutable tag a build pushes: the commit's first 12 hex chars; a dirty tree's adds its UTC build time (two
    dirty builds of one commit differ)."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"{sha!r}: expected a full lowercase git sha")
    return f"{sha[:12]}-dirty-{now.astimezone(timezone.utc):%Y%m%dt%H%M%Sz}" if dirty else sha[:12]


def check_tag(cfg: BuildConfig, tag: str) -> str:
    """An explicit candidate tag (`-t`): a lowercase Docker tag, and never one of the live tags (a candidate is built to
    its own tag, then promoted)."""
    if not TAG.fullmatch(tag):
        raise ClickException(f"-t {tag!r}: expected a lowercase Docker tag of at most 128 characters")
    if tag in cfg.live_tags:
        raise ClickException(f"-t {tag}: a live tag; build a candidate, then `promote` it")
    return tag


def parse_build_args(pairs: Sequence[str]) -> dict[str, str]:
    """`-a KEY=VALUE` overrides of the config's `build_args`."""
    out = {}
    for p in pairs:
        k, eq, v = p.partition("=")
        if not eq or not ARG_NAME.fullmatch(k) or k in STAMP_ARGS:
            raise ClickException(f"-a {p!r}: expected KEY=VALUE, KEY an upper-case ARG name other than {', '.join(STAMP_ARGS)}")
        out[k] = v
    return out


def cloudbuild_config(cfg: BuildConfig, images: Sequence[str], sha: str, dirty: bool, build_args: Mapping[str, str]) -> dict:
    """The Cloud Build config: one docker step building the staged context's Dockerfile, stamped, tagged as every
    image in `images`, which Cloud Build then pushes. Every value is baked in (no substitutions), shell-quoted."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"{sha!r}: an unstamped build")
    q = shlex.quote
    args = [f"--build-arg {q(f'{k}={v}')}" for k, v in {**build_args, "DISKY_GIT_SHA": sha, "DISKY_GIT_DIRTY": str(int(dirty))}.items()]
    lines = ["set -euo pipefail"]
    if cfg.cache:
        cache = f"{cfg.repository}:{cfg.live_tags[0]}"
        lines.append(f"docker pull {q(cache)} || true")
        args += ["--build-arg BUILDKIT_INLINE_CACHE=1", f"--cache-from {q(cache)}"]
    lines.append(" ".join(["DOCKER_BUILDKIT=1 docker build", f"-f {q(cfg.dockerfile)}", *args, *(f"-t {q(i)}" for i in images), "."]))
    script = "\n".join(lines)
    if "$" in script:
        raise ValueError("a build value carries `$`, which Cloud Build would read as a substitution")
    out: dict = {"steps": [{"name": DOCKER, "entrypoint": "bash", "args": ["-c", script]}], "images": list(images)}
    cb = cfg.cloudbuild
    options = {k: v for k, v in (("machineType", cb.machine_type), ("logging", cb.logging)) if v}
    if options:
        out["options"] = options
    if cb.timeout:
        out["timeout"] = cb.timeout
    if cb.tags:
        out["tags"] = list(cb.tags)
    return out


def submit_command(cfg: BuildConfig, context: Path) -> list[str]:
    cb = cfg.cloudbuild
    cmd = ["gcloud", "builds", "submit", str(context), "--project", cfg.project, "--config", str(context / "cloudbuild.json")]
    if cb.region:
        cmd += ["--region", cb.region]
    if cb.service_account:
        cmd += ["--service-account", f"projects/{cfg.project}/serviceAccounts/{cb.service_account}"]
    if cb.staging_dir:
        cmd += ["--gcs-source-staging-dir", cb.staging_dir]
    return cmd


def describe_command(image: str) -> list[str]:
    return ["gcloud", "artifacts", "docker", "images", "describe", image, "--format=value(image_summary.digest)"]


def tag_command(repository: str, digest: str, tag: str) -> list[str]:
    return ["gcloud", "artifacts", "docker", "tags", "add", f"{repository}@{digest}", f"{repository}:{tag}", "--quiet"]


def repoint_commands(cfg: BuildConfig) -> list[tuple[list[str], list[str]]]:
    """Each Cloud Run consumer's (existence check, update) pair: at the primary repository's first live tag."""
    rp = cfg.repoint
    if rp is None:
        return []
    image = f"{cfg.repository}:{cfg.live_tags[0]}"
    loc = ["--project", cfg.project, "--region", rp.region]
    out = []
    for kind, names, fmt in (("jobs", rp.jobs, "value(name)"), ("services", rp.services, "value(metadata.name)")):
        for n in names:
            out.append((["gcloud", "run", kind, "describe", n, *loc, f"--format={fmt}"],
                        ["gcloud", "run", kind, "update", n, *loc, "--image", image, "--quiet"]))
    return out


class Runner:
    """Runs (or, dry, prints) the build's commands. `capture` returns `(exit code, stdout)`; dry, every lookup misses."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def run(self, cmd: list[str]) -> None:
        err("+", shlex.join(cmd))
        if not self.dry_run:
            subprocess.run(cmd, check=True, stdout=sys.stderr)

    def capture(self, cmd: list[str]) -> tuple[int, str]:
        if self.dry_run:
            err("+", shlex.join(cmd))
            return 1, ""
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout.strip()


def digest_of(runner: Runner, image: str) -> str | None:
    """`image`'s manifest digest in Artifact Registry, None when it has none (no such tag)."""
    rc, out = runner.capture(describe_command(image))
    if rc != 0 or not out:
        return None
    if not DIGEST.fullmatch(out):
        raise ClickException(f"{image}: unexpected digest {out!r}")
    return out


def promote(cfg: BuildConfig, runner: Runner, tag: str, expect: str | None = None) -> dict[str, str]:
    """Move each repository's live tags to the digest `tag` names there, then re-point the Cloud Run consumers.
    Returns `{repository: digest}`."""
    if not cfg.live_tags:
        raise ClickException("nothing to promote: the config has no live_tags (its consumers pin a digest)")
    digests = {}
    for repo in cfg.repositories:
        d = digest_of(runner, f"{repo}:{tag}")
        if d is None:
            if runner.dry_run:
                d = "sha256:" + "0" * 64
            else:
                raise ClickException(f"{repo}:{tag}: no such image (build the candidate first)")
        digests[repo] = d
    if expect is not None and digests[cfg.repository] != expect and not runner.dry_run:
        raise ClickException(f"{cfg.repository}:{tag} is {digests[cfg.repository]}, not the expected {expect}")
    for repo, d in digests.items():
        for live in cfg.live_tags:
            runner.run(tag_command(repo, d, live))
    for check, update in repoint_commands(cfg):
        rc, _ = runner.capture(check)
        if rc == 0 or runner.dry_run:
            runner.run(update)
        else:
            err(f"skipped: {check[2]} {check[4]} doesn't exist")
    return digests


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def source_revision(root: Path) -> tuple[str, list[str]]:
    """`root`'s HEAD sha and its tracked files' uncommitted changes (`git status --porcelain` lines)."""
    sha = git(root, "rev-parse", "HEAD").strip()
    changes = [line for line in git(root, "status", "--porcelain", "--untracked-files=no").splitlines() if line]
    return sha, changes


def stage_context(root: Path, cfg: BuildConfig, sha: str, dirty: bool, dest: Path) -> None:
    """The build context in `dest`: the commit's files under the config's pathspecs (dirty: the worktree's tracked
    ones), plus an empty `.gcloudignore` (the upload is exactly the staged files)."""
    dest.mkdir(parents=True)
    if dirty:
        files = [f for f in git(root, "ls-files", "-z", "--", *cfg.context).split("\0") if f]
        for f in files:
            src = root / f
            if src.is_file():
                (dest / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / f)
    else:
        archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", sha, "--", *cfg.context], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    (dest / ".gcloudignore").write_text("# the context is staged by `dt-cloud image build`: upload all of it\n")
    if not (dest / cfg.dockerfile).is_file():
        raise ClickException(f"{cfg.dockerfile}: not in the staged context (add it to the config's `context`)")


def build(cfg: BuildConfig, root: Path, runner: Runner, *, mode: str, tag: str | None, allow_dirty: bool,
          build_args: Mapping[str, str], now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict[str, str]:
    """Build (or find) the candidate, print each repository's `<repository>@<digest>`, and in `live` mode promote it."""
    sha, changes = source_revision(root)
    dirty = bool(changes)
    err(f"building checkout {root}: sha {sha}, dirty {int(dirty)}")
    for line in changes:
        err(f"  {line}")
    if dirty and not cfg.allow_dirty:
        raise ClickException("refusing a dirty tree: this deployment builds only committed source")
    if dirty and not allow_dirty:
        raise ClickException("refusing a dirty tree: commit (or -D to build it stamped dirty)")
    args = {**cfg.build_args, **build_args}
    if build_args and tag is None:
        raise ClickException("-a changes the image without changing the commit: name its candidate with -t")
    tag = check_tag(cfg, tag) if tag is not None else candidate_tag(sha, dirty, now())
    images = [f"{repo}:{tag}" for repo in cfg.repositories]
    existing = digest_of(runner, images[0])
    if existing is not None:
        err(f"{images[0]} exists ({existing}): not rebuilt")
    else:
        context = root / "tmp" / f"image-build-{now().astimezone(timezone.utc):%Y%m%dT%H%M%S%fZ}"
        stage_context(root, cfg, sha, dirty, context)
        (context / "cloudbuild.json").write_text(json.dumps(cloudbuild_config(cfg, images, sha, dirty, args), indent=1) + "\n")
        err(f"building {', '.join(images)} (Cloud Build, project {cfg.project}; context {context})")
        try:
            runner.run(submit_command(cfg, context))
        finally:
            if not runner.dry_run:
                shutil.rmtree(context, ignore_errors=True)
    digests = {}
    for image, repo in zip(images, cfg.repositories):
        d = digest_of(runner, image)
        if d is None:
            if not runner.dry_run:
                raise ClickException(f"{image}: pushed, but no digest found")
            d = "sha256:<digest>"
        digests[repo] = d
        print(f"{repo}@{d}", flush=True)
    err(f"candidate tag: {tag}")
    if mode == "live":
        promote(cfg, runner, tag, expect=None if runner.dry_run else digests[cfg.repository])
    return digests


def toplevel(path: Path) -> Path | None:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def resolve(config: str | None, other_checkout: bool) -> tuple[Path, BuildConfig]:
    """The checkout to image (the config's) and its config; refused when the caller's cwd is in another checkout."""
    cwd_root = toplevel(Path.cwd())
    path = Path(config) if config else (cwd_root or Path.cwd()) / "job" / "build.json"
    root = toplevel(path.resolve().parent)
    if root is None:
        raise ClickException(f"{path}: not in a git checkout")
    if cwd_root is not None and cwd_root != root and not other_checkout:
        raise ClickException(f"cwd is in checkout {cwd_root}, but this config images {root}: run {cwd_root}/job/build.sh "
                             f"to image your checkout (-O to build {root} anyway)")
    return root, load_config(path)


@group("image")
def cli() -> None:
    """The scan job image: build a stamped candidate, promote it to the live tags (specs/job-consolidation.md §5)."""


config_opt = option("-c", "--config", type=ClickPath(dir_okay=False), help="The deployment's build config (default: <checkout>/job/build.json)")
dry_opt = option("-n", "--dry-run", is_flag=True, help="Print the commands (and keep the staged context under tmp/), run nothing")
other_opt = option("-O", "--other-checkout", is_flag=True, help="Build the config's checkout though the cwd is in another one")


@cli.command("build")
@option("-a", "--build-arg", "build_args", multiple=True, help="KEY=VALUE over the config's build_args (repeatable; needs -t)")
@config_opt
@option("-D", "--allow-dirty", is_flag=True, help="Build a tree with uncommitted changes to tracked files (stamped dirty)")
@option("-m", "--mode", type=Choice(MODES), help="candidate: an immutable tag, nothing live moves; live: then promote it (default: the config's mode)")
@dry_opt
@other_opt
@option("-t", "--tag", help="The candidate's tag (default: the commit's first 12 hex chars)")
def build_cmd(build_args, config, allow_dirty, mode, dry_run, other_checkout, tag) -> None:
    """Build this checkout's image and print `<repository>@<digest>` per repository (primary first)."""
    root, cfg = resolve(config, other_checkout)
    build(cfg, root, Runner(dry_run), mode=mode or cfg.mode, tag=tag, allow_dirty=allow_dirty, build_args=parse_build_args(build_args))


@cli.command("promote")
@config_opt
@dry_opt
@other_opt
@option("-x", "--expect", help="Refuse unless the primary repository's TAG is this digest (sha256:…)")
@argument("tag")
def promote_cmd(config, dry_run, other_checkout, expect, tag) -> None:
    """Move the config's live tags to candidate TAG's image in every repository, then re-point its Cloud Run consumers."""
    if expect is not None and not DIGEST.fullmatch(expect):
        raise ClickException(f"-x {expect!r}: expected sha256:<64 hex>")
    _, cfg = resolve(config, other_checkout)
    for repo, d in promote(cfg, Runner(dry_run), tag, expect).items():
        print(f"{repo}@{d}")
