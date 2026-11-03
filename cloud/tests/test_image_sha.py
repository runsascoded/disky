"""`dt-cloud image-sha [-e SHA]` and the harnesses' `-E/--expect-sha`: the image's `job/build.sh` stamp
(`$DISKY_GIT_SHA` / `$DISKY_GIT_DIRTY`) printed, and checked against the build a run means to measure."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from dt_cloud import image_sha
from dt_cloud.cli import main
from dt_cloud.image_sha import Stamp, mismatch, stamp

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"


def env(sha: str | None, dirty: str | None = "0") -> dict[str, str | None]:
    return {"DISKY_GIT_SHA": sha, "DISKY_GIT_DIRTY": dirty, "BATCH_TASK_INDEX": None}


def run(args: list[str], e: dict, cli=main):
    res = CliRunner(env=e).invoke(cli, args, catch_exceptions=False)
    return res.exit_code, res.output.splitlines()


@pytest.fixture(autouse=True)
def fresh_log(monkeypatch):
    """`log_stamp` logs once per process; each test sees its first line."""
    monkeypatch.setattr(image_sha, "_logged", False)


@pytest.mark.parametrize("e, want", [
    (env(SHA), Stamp(SHA, False)),
    (env(SHA.upper()), Stamp(SHA, False)),
    (env(SHA, "1"), Stamp(SHA, True)),
    (env(SHA, None), Stamp(SHA, None)),
    (env(None), Stamp(None, None)),
    (env(""), Stamp(None, None)),
])
def test_stamp(e, want):
    assert stamp({k: v for k, v in e.items() if v is not None}) == want


@pytest.mark.parametrize("e, out", [
    (env(SHA), [SHA]),
    (env(SHA, "1"), [f"{SHA} dirty"]),
    (env(SHA, None), [f"{SHA} dirty-unknown"]),
    (env(None), ["unstamped"]),
    (env(""), ["unstamped"]),
])
def test_print(e, out):
    assert run(["image-sha"], e) == (0, out)


def test_print_json():
    assert run(["image-sha", "-j"], env(SHA, "1")) == (0, [f'{{"sha": "{SHA}", "dirty": true}}'])
    assert run(["image-sha", "-j"], env(None)) == (0, ['{"sha": null, "dirty": null}'])


@pytest.mark.parametrize("e, args, code, out", [
    # the full sha, and prefixes of it (≥ 7 chars, any case)
    (env(SHA), ["-e", SHA], 0, [SHA]),
    (env(SHA), ["-e", SHA[:7]], 0, [SHA]),
    (env(SHA), ["-e", SHA[:12].upper()], 0, [SHA]),
    # another build
    (env(OTHER), ["-e", SHA[:7]], 1, [OTHER, f"image-sha: image is {OTHER}, expected {SHA[:7]}"]),
    # the right sha, but built from uncommitted changes (or not saying)
    (env(SHA, "1"), ["-e", SHA], 1, [f"{SHA} dirty", f"image-sha: image {SHA} is dirty: built from uncommitted changes, not exactly {SHA} (--allow-dirty to accept)"]),
    (env(SHA, "1"), ["-e", SHA, "-a"], 0, [f"{SHA} dirty"]),
    (env(SHA, None), ["-e", SHA], 1, [f"{SHA} dirty-unknown", f"image-sha: image {SHA} carries no $DISKY_GIT_DIRTY: it may hold uncommitted changes (--allow-dirty to accept)"]),
    # an image build.sh didn't stamp
    (env(None), ["-e", SHA], 1, ["unstamped", f"image-sha: image is unstamped (no $DISKY_GIT_SHA); expected {SHA}: rebuild it with job/build.sh"]),
    (env(None), ["-e", SHA, "-a"], 1, ["unstamped", f"image-sha: image is unstamped (no $DISKY_GIT_SHA); expected {SHA}: rebuild it with job/build.sh"]),
])
def test_expect(e, args, code, out):
    assert run(["image-sha", *args], e) == (code, out)


@pytest.mark.parametrize("bad", [SHA[:6], "g123456", SHA + "0"])
def test_expect_needs_a_sha_or_7_char_prefix(bad):
    code, out = run(["image-sha", "-e", bad], env(SHA))
    assert (code, out[-1]) == (2, f"Error: Invalid value for '-e' / '--expect': {bad!r}: expected a git sha or a prefix of at least 7 hex chars")


def test_mismatch():
    assert [mismatch(s, SHA[:7]) for s in (Stamp(SHA, False), Stamp(OTHER, False))] == [None, f"image is {OTHER}, expected {SHA[:7]}"]


def log_line(label: str, sha: str) -> str:
    return f"{label}: image {sha} (dt_cloud: {image_sha.code_dir()})"
